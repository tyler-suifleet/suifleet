# meta-suifleet

Software layer for the SuiFleet Linux edge daemon, isolated signing service,
first-boot provisioning and optional keyless Walrus archive runtime.
Maintainer: Tyler Bush <tyler.jonathan.bush@gmail.com>.
Metadata is MIT licensed (COPYING.MIT); each recipe declares its payload licenses.

## Availability and supported configurations

This is a prepared standalone export, **not a published standalone repository or
an OpenEmbedded Layer Index listing**. The authoritative development repository is
https://github.com/tyler-suifleet/suifleet (access may be restricted).
The packaged daemon is deliberately pinned to 0.2.1 at
`2d231cadf3c0c8e58b48dc9f48dd7890faaafa1f`, not the latest development client.
Do not advertise current-client feature completeness for this pin.

Declared series: scarthgap and wrynose, using only the full revisions in the
core repository's `kas/packaging.yml` and `kas/packaging-wrynose.yml`.
Scarthgap requires the pinned meta-lts-mixins scarthgap/rust toolchain;
Wrynose uses core Rust. Rust >= 1.90 is checked by the recipe. Dependencies are
OE-Core (`core`) and meta-openembedded/meta-oe (`openembedded-layer`). Systemd,
usrmerge and a writable persistent root are the validated image configuration.
The archive runtime's prebuilt Node recipe supports Linux AArch64/glibc only.
There are no standalone release branches yet; series compatibility is not proof
that a same-named remote branch exists. No other release/architecture is claimed.

Add this directory to BBLAYERS and build `bitbake suifleet`. Optional archive
packages are `suifleet-node` and `suifleet-walrus-codec`; the codec requires five
prebuilt, checksum-pinned files at SUIFLEET_WALRUS_CODEC_DIR **before parsing**.
See RELEASE.md for source/artifact publication prerequisites.

The separate, core-repository `meta-suifleet-integration` contains Pi image/access
policy, test components and core-recipe build workarounds. Existing kas profiles
include both layers. Generic consumers should select their own BSP/distro and
must not add the integration layer without reviewing its policy.

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
