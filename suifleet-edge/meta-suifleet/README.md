# meta-suifleet

OpenEmbedded software layer for the SuiFleet edge daemon, isolated signing
service, first-boot provisioning and optional Walrus archive runtime.

Maintainer: Tyler Bush <tyler.jonathan.bush@gmail.com>.
Metadata is MIT licensed ([COPYING.MIT](COPYING.MIT)). Each recipe declares
its payload licenses.

## Build

Select the branch for your OpenEmbedded series. The tested configuration is
ARM64 with systemd, usrmerge, glibc and a writable persistent root filesystem.
Scarthgap requires the pinned meta-lts-mixins `scarthgap/rust` toolchain.
Wrynose uses core Rust. Use the full upstream revisions from the core
repository's [kas profiles](https://github.com/tyler-suifleet/suifleet/tree/main/kas).
Add OE-Core and meta-openembedded's `meta-oe` to BBLAYERS, add this checkout
with `bitbake-layers add-layer /path/to/meta-suifleet`, and run:

```sh
bitbake suifleet
```

Include `suifleet` in your image packages. Build and include
`suifleet-walrus-codec` if you need Walrus archives. It pulls in
`suifleet-node`, which supports ARM64/glibc. Both source distributions are
downloaded and checksum-verified by BitBake. Ordinary parsing and daemon
builds need no locally generated codec.

The core repository's separate `meta-suifleet-integration` contains the Pi
image and access policy, test components and distro-specific workarounds.
Generic consumers can select their own BSP and distro.
See the [device install guide](https://github.com/tyler-suifleet/suifleet/blob/main/docs/INSTALL_DEVICE_CLIENT.md#yocto-image-and-recipe)
for provisioning. Builds ship examples. Generate keys and provision enrollment
at runtime. Preserve `/etc/suifleet` and `/var/lib/suifleet` across updates.
Platform tools own OS installation, reboot, boot confirmation and rollback.

## Contributions

Send patches through pull requests and reference the core issue where applicable.
Keep source revision, crate checksums, lock digest and codec provenance in sync.
Include exact recipe and layer checks for affected series, with build limits.
Keep credentials and live device state out of source and artifacts.
See [RELEASE.md](RELEASE.md) for release and Layer Index details.
