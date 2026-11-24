# meta-suifleet

Software layer for the SuiFleet Linux edge daemon, isolated signing service,
first-boot provisioning and optional keyless Walrus archive runtime.
Maintainer: Tyler Bush <tyler.jonathan.bush@gmail.com>.
Metadata is MIT licensed (COPYING.MIT); each recipe declares its payload licenses.

## Availability and supported configurations

This is a prepared standalone export, **not a published standalone repository or
an OpenEmbedded Layer Index listing**. The authoritative development repository is
https://github.com/tyler-suifleet/suifleet (public).
The packaged daemon is deliberately pinned to 0.2.3 at
`b9bb2ef6210624d732ab2de2075ea1826e5300c1`, not the latest development client.
Do not advertise current-client feature completeness for this pin.

Declared series: scarthgap and wrynose, using only the full revisions in the
core repository's `kas/packaging.yml` and `kas/packaging-wrynose.yml`.
Scarthgap requires the pinned meta-lts-mixins scarthgap/rust toolchain;
Wrynose uses core Rust. Rust >= 1.90 is checked by the recipe. Dependencies are
OE-Core (`core`) and meta-openembedded/meta-oe (`openembedded-layer`). Systemd,
usrmerge and a writable persistent root were used in the validated integrated
builds described below.
The archive runtime's prebuilt Node recipe supports Linux AArch64/glibc only.
There are no standalone release branches yet; series compatibility is not proof
that a same-named remote branch exists. No other release/architecture is claimed.

Add this directory to BBLAYERS and run `bitbake suifleet`. Optional archive
packages are `suifleet-node` and `suifleet-walrus-codec`; the codec requires five
prebuilt, checksum-pinned files at SUIFLEET_WALRUS_CODEC_DIR **before parsing**.
See RELEASE.md for source/artifact publication prerequisites.

The separate, core-repository `meta-suifleet-integration` contains Pi image/access
policy, test components and a glibc usrmerge layout guard for its reused build
caches. Existing kas profiles include both layers. Generic consumers should select
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

- `openssl`, all variants: `PARALLEL_MAKEINST = "-j1"`. This works around a
  recursive `install_sw` relink race. The setting does not change task signatures.
- `libcap`, Wrynose: passes the cross toolchain to the install-time `capsh`
  relink, which otherwise falls back to the host gcc.
- `xz`, Wrynose target: points `xzmore` at `${bindir}/bash` and adds bash to
  RDEPENDS, which satisfies file-rdeps package QA.

Each was still unfixed on OE-Core scarthgap, wrynose and master on 2026-11-24.
The integration layer inherits the same class, so the pinned profiles keep
identical task signatures. Validation, including builds from the extracted
export without the integration layer, is recorded in the core repository's
`docs/YOCTO_STANDALONE_VALIDATION.md`. Apart from the class, the standalone
configuration requires OE-Core, meta-oe, the Scarthgap Rust mixin, and the
systemd/usrmerge distro features listed above. It needs no other distro setting.

Builds contain examples only: no device keys, enrollment configuration, signer
PINs or journals. Provision identities at runtime. Preserve /etc/suifleet and
/var/lib/suifleet across upgrades. Platform tools own OS installation, reboot and
firmware recovery; the daemon does not establish healthy boot.

## Contributions

Send patches through pull requests. Until standalone publication, open changes against the existing SuiFleet
repository and reference its existing issue. Never include credentials or live
state. Keep source revision, crate include, lock digest and codec provenance in
sync; retain signing/identity boundaries and package QA. Supply exact recipe
build and yocto-check-layer logs for both declared series, artifact checksums and
limitations. Hardware, mainnet and production-provider acceptance are separate.
See RELEASE.md for export and submission procedure.
