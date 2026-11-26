# Integrate SuiFleet into an OpenEmbedded image

Use the [public meta-suifleet repository](https://github.com/tyler-suifleet/meta-suifleet)
for the standalone software layer. It supplies the edge client, isolated signer,
systemd units and first-boot key provisioning. Your product supplies the BSP,
kernel, storage layout, distro policy, network and access configuration.

The public repository publishes `kirkstone`, `scarthgap` and `wrynose`
branches. Pin a full commit for your series. OpenEmbedded Layer Index
submission is pending. See the [layer release checklist](https://github.com/tyler-suifleet/meta-suifleet/blob/main/RELEASE.md)
for the source distribution and branch validation requirements.

## Prerequisites

Start with a clean build directory on a host supported by your OE release.
Use the upstream revisions in the matching [pinned engineering profile](../kas/):

| Series | Profile | Rust toolchain |
| --- | --- | --- |
| Scarthgap | `packaging.yml` | `meta-lts-mixins` on `scarthgap/rust` |
| Wrynose | `packaging-wrynose.yml` | OE-Core |
| Kirkstone | `packaging-kirkstone.yml` | `meta-lts-mixins` on `kirkstone/rust` |

Use matching OE-Core and `meta-openembedded/meta-oe` revisions. Keep each
series in its own build directory and TMPDIR. The generic example below
uses Scarthgap. Read the [legacy series guidance](YOCTO_LEGACY_SERIES.md)
before choosing Kirkstone for a product.

## Add the software layer

From an initialized product build shell, set `SUIFLEET_LAYER_COMMIT` to the
full commit from the matching
profile or your validated product pin. Set `SUIFLEET_LAYER_DIR` to an
absolute path outside the build directory. Then run:

```sh
git clone https://github.com/tyler-suifleet/meta-suifleet.git "$SUIFLEET_LAYER_DIR"
git -C "$SUIFLEET_LAYER_DIR" checkout --detach "${SUIFLEET_LAYER_COMMIT:?set the verified layer commit}"
test "$(git -C "$SUIFLEET_LAYER_DIR" rev-parse HEAD)" = "$SUIFLEET_LAYER_COMMIT"
bitbake-layers add-layer "$SUIFLEET_LAYER_DIR"
bitbake-layers show-layers
bitbake-layers show-recipes suifleet
```

Confirm `BBLAYERS` includes core, `meta-oe`, the Scarthgap mixin where
required, your product layers and the root of the standalone checkout.
The generic example uses neither product companion layer from the core
repository. Set init, package and access policy in your product distro or image.

## Generic ARM64 QEMU example

In a disposable stock OE build with the matching layers, put this in
`conf/local.conf`. Keep your product's own machine and image recipe when
applying the same package selection to a product build.

```bitbake
MACHINE = "qemuarm64"
DISTRO = "poky"
DISTRO_FEATURES:append = " systemd usrmerge"
DISTRO_FEATURES_BACKFILL_CONSIDERED:append = " sysvinit"
VIRTUAL-RUNTIME_init_manager = "systemd"
VIRTUAL-RUNTIME_initscripts = ""
IMAGE_INSTALL:append = " suifleet"
IMAGE_FSTYPES = "ext4"
INHERIT += "suifleet-core-fixes"
RUST_LLVM_TARGETS = "AArch64;X86"
QEMU_TARGETS = "aarch64"
PACKAGECONFIG:remove:pn-qemu-system-native = "virglrenderer epoxy"
```

The opt-in class applies the OE-Core recipe fixes listed in the [layer README](https://github.com/tyler-suifleet/meta-suifleet/blob/scarthgap/README.md).
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
bitbake core-image-minimal qemu-system-native
runqemu qemuarm64 core-image-minimal ext4 nographic slirp
```

For the Scarthgap disposable console test, add
`EXTRA_IMAGE_FEATURES:append = " debug-tweaks"` before the image build.
Use your product login policy for distributed images.

## Raspberry Pi 3 Model B+ example

Use the separate [Pi image guide](CUSTOMER_PI_IMAGE.md) for the committed kas
profile with the Raspberry Pi BSP, SD image settings and access overlay. That
profile starts from the core checkout and pulls in the public software layer
plus the integration and enrollment companion layers. Its
BSP and distro settings do not carry over to QEMU or a new board. New boards
need their own BSP and runtime validation. Physical Pi acceptance remains open.
Userspace vintage and the running kernel are independent. Record `uname -r`
and check systemd and kernel prerequisites. The signer's strong caller
binding requires `SO_PEERPIDFD`. A Kirkstone userspace on a newer kernel
can have different signer behavior from the same userspace on a 5.15 kernel.

## Expected result and first-boot enrollment

1. Record your layer commits, toolchain version and the recipe selected by
   `bitbake-layers show-recipes suifleet`. BitBake verifies the public source
   archive against the recipe's SHA-256 during fetch. Compare the source revision,
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
   registered device on the selected network. The public layer at
   `c78e6297691b38da87989a7ec279734a6669e379` supports registration by
   address and the explicit `suifleet --enroll` command.
   For automatic first-boot enrollment, add
   `suifleet-edge/meta-suifleet-enrollment` from the core checkout to
   `BBLAYERS` and use the [enrollment guide](CUSTOMER_ENROLLMENT.md).
5. Preserve `/etc/suifleet` and `/var/lib/suifleet` with their owners and
   modes across image replacement and rollback. Use the
   [in-place migration procedure](INSTALL_DEVICE_CLIENT.md#migrate-an-enrolled-linux-device-in-place)
   when moving an existing device to the enrollment companion packaging. Verify the address and journals
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
