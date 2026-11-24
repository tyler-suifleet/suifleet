# Integrate SuiFleet into an OpenEmbedded image

Use the [public meta-suifleet repository](https://github.com/tyler-suifleet/meta-suifleet)
for the standalone software layer. It supplies the edge client, isolated signer,
systemd units and first-boot key provisioning. Your product supplies the BSP,
kernel, storage layout, distro policy, network and access configuration.

Repository access, supported release refs and an OpenEmbedded Layer Index
listing are separate milestones. Use a full layer commit backed by successful
fetch, build and image checks for your series. Check the repository's
[release checklist](https://github.com/tyler-suifleet/meta-suifleet/blob/main/RELEASE.md)
and ask your release operator for that commit and evidence. Availability of
`main` or a source archive alone does not establish a validated release.

## Prerequisites

Start with a clean build directory on a host supported by your OE release.
Select matching OE-Core and `meta-openembedded/meta-oe` revisions. Scarthgap
also needs the `meta-lts-mixins` Rust layer from `scarthgap/rust`. Wrynose uses
core Rust. The [pinned engineering profiles](../kas/) record exact upstream
revisions in `packaging.yml` and `packaging-wrynose.yml`. Keep the series in
separate build directories and TMPDIRs.

## Add the software layer

From an initialized product build shell, set `SUIFLEET_LAYER_COMMIT` to the
verified full commit from your operator and `SUIFLEET_LAYER_DIR` to an absolute
path outside the build directory. Then run:

```sh
git clone https://github.com/tyler-suifleet/meta-suifleet.git "$SUIFLEET_LAYER_DIR"
git -C "$SUIFLEET_LAYER_DIR" checkout --detach "${SUIFLEET_LAYER_COMMIT:?set the verified layer commit}"
test "$(git -C "$SUIFLEET_LAYER_DIR" rev-parse HEAD)" = "$SUIFLEET_LAYER_COMMIT"
bitbake-layers add-layer "$SUIFLEET_LAYER_DIR"
bitbake-layers show-layers
```

Confirm `BBLAYERS` includes core, `meta-oe`, the Scarthgap mixin where
required, your product layers and the root of the standalone checkout.
For the generic example omit `meta-suifleet-integration`. Use any
series-specific distro fixes documented and validated for the selected
layer revision. The standalone layer does not select your distro policy.

## Generic ARM64 QEMU example

In a disposable stock OE build with the matching layers, put this in
`conf/local.conf`. Keep your product's own machine and image recipe when
applying the same package selection to a product build.

```bitbake
MACHINE = "qemuarm64"
INIT_MANAGER = "systemd"
DISTRO_FEATURES:append = " usrmerge"
IMAGE_INSTALL:append = " suifleet"
IMAGE_FSTYPES:append = " ext4"
```

Use glibc and persistent writable storage for `/etc/suifleet` and
`/var/lib/suifleet`. For a product image, put the `IMAGE_INSTALL` line in
the image recipe or use the product's existing packagegroup. The
`suifleet` package includes the signer and provisioning units. Add
`suifleet-walrus-codec` only for Walrus archive workflows. It pulls in
`suifleet-node` and requires ARM64/glibc. Neither is needed for the minimal image.

```sh
bitbake -p
bitbake -c fetch suifleet
bitbake suifleet
oe-pkgdata-util list-pkg-files suifleet
bitbake core-image-minimal
runqemu qemuarm64 core-image-minimal ext4 nographic
```

If you need passwordless console access during emulation, configure it in
a disposable test image using the option documented by your OE series.
Apply your production login policy before distribution.

## Raspberry Pi 3 Model B+ example

Use the separate [Pi image guide](CUSTOMER_PI_IMAGE.md) for the committed kas
profile with the Raspberry Pi BSP, SD image settings and access overlay. That
profile is built from the core checkout and includes the integration layer. Its
BSP and distro settings do not carry over to QEMU or a new board. New boards
need their own BSP and runtime validation. Physical Pi acceptance remains open.
Userspace vintage and the running kernel are independent. Record `uname -r`
and check systemd and kernel prerequisites against the runtime guidance
before claiming a new target works.

## Expected result and first-boot enrollment

1. Record your layer commits, toolchain version and the recipe selected by
   `bitbake-layers show-recipes suifleet`. Compare the source revision,
   Cargo lock digest and client version in `/usr/share/suifleet/build-info`
   with the recipe and the fleet manifest's supported client range. Keep
   the source pin, generated crate include and lock digest synchronized.
2. Inspect the deployed image manifest for `suifleet`. Check the rootfs
   contains `/usr/bin/suifleet`, the daemon and provisioning services, the
   signer socket and service, the enrollment path unit and configuration
   templates. Confirm there is no device key, live configuration, enrollment
   credential or journal in the image.
3. On the target, first-boot provisioning generates the device key. Run
   `systemctl status suifleet-provision.service` and obtain only the public
   address from `journalctl -u suifleet-provision.service`. Follow the
   [device setup guide](INSTALL_DEVICE_CLIENT.md#find-the-device-address)
   to register or enroll that address on the correct network.
4. On the running target install the authenticated `deployment.json`, then
   install the signer configuration as `root:suifleet-signer` mode `0640`.
   Install `config.toml` last as `root:suifleet` mode `0640`. Use the
   packaged examples and the installation guide's exact paths and fields.
   Check `systemctl status suifleet.service suifleet-signer.socket` and the
   daemon journal for successful startup. Confirm the console sees the
   registered device on the selected network.
5. Preserve `/etc/suifleet` and `/var/lib/suifleet` with their owner and mode
   across image replacement and rollback. Verify the address and journals
   survive an emulated restart or update. Your platform owns updater selection,
   OS installation, reboot, boot confirmation and firmware rollback.

## Recovery and limits

If a build fails, record the layer and source pins with the failing task.
Check that the layer matches the series and that all pinned sources are
available. If startup fails after enrollment, check unit status, configuration
permissions and manifest compatibility before changing device state.

Record parse, source fetch, package QA, image build and boot separately.
An emulated boot establishes behavior for that emulated image. Physical boot,
power-loss and board-level acceptance are tracked in
[physical target acceptance](https://github.com/tyler-suifleet/suifleet/issues/32).
## Technical reference

See [packaging and validation details](YOCTO_PACKAGING.md) for provenance and
[standalone layer release tracking](https://github.com/tyler-suifleet/suifleet/issues/820) for release-ref
and Layer Index readiness.
