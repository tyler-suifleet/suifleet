# Yocto client packaging

SF-025 packages the daemon and first-boot/enrollment services. SF-029 supplies
the physical Raspberry Pi image/test deployment workflow; SF-031 owns live OTA
acceptance. A boot test in QEMU is not evidence of Pi firmware, power-loss or
public-network acceptance.

## Pinned build inputs

Use Scarthgap plus the user's selected `meta-lts-mixins` `scarthgap/rust`
layer. Plain Scarthgap Rust 1.75 cannot compile this lockfile. The committed
[kas packaging profile](../kas/packaging.yml) pins Poky, meta-openembedded,
meta-raspberrypi and the Rust mixin to full Git revisions. The mixin revision
`eedcdc7486ff15c3510c2ceff68c87d9db141312` selects Rust/Cargo 1.98.1.
LLVM backends are limited to AArch64 and X86 for this ARM64 target/x86 build host.
Other hosts/architectures need a deliberate profile change and validation.

The local SuiFleet layer comes from this checkout. Archive its commit ID and
any diff alongside the kas configuration and build artifacts. The daemon source
is fetched independently at the recipe's fixed `SRCREV`
`c10ac76e9aae5b3ea8f92a42d8a68068c9bba71f`. This is the pre-SF-004 client:
packaging work does not establish supported public-network transport. Bump the
source pin and crate graph together after SF-004 integration, before SF-031.
The pre-migration daemon also lacks the corrected real-node dynamic-field read
path; the local HTTP simulator does not establish real-chain OTA acceptance.

`suifleet-crates.inc` declares every registry package/version and SHA-256 from
that revision's Cargo.lock. BitBake fetches/unpacks them before compilation;
Yocto's cargo class vendors them locally and passes `--frozen`. The recipe also
sets `CARGO_NET_OFFLINE=true` and checks the lockfile digest before configure.
There is no compile-time crate download or `AUTOREV`.

To update: select a reviewed source revision, update `SRCREV`, run
`bitbake -c update_crates suifleet`, and update `SUIFLEET_LOCK_SHA256` from that
revision's `suifleet-edge/Cargo.lock`. Review the generated include and build
the package/image again. Yocto documents this workflow in its
[Cargo classes reference](https://docs.yoctoproject.org/5.0.17/ref-manual/classes.html#cargo-update-recipe-crates).
Do not merely disable the digest check to accommodate a lockfile change.

## Build

From the repository with kas 4.7 and the Scarthgap host prerequisites:

```sh
# Keep downloaded layers and build outputs outside the checkout.
export KAS_WORK_DIR=/absolute/path/to/yocto-work
export KAS_BUILD_DIR="$KAS_WORK_DIR/build-pi"
kas build kas/packaging.yml

# Disposable emulation image; debug-tweaks allows passwordless console login.
# Do not use this test profile for a deployed/production device.
export KAS_BUILD_DIR="$KAS_WORK_DIR/build-qemu"
kas build kas/packaging-qemu.yml
```

The base profile cross-builds the `suifleet` package for `raspberrypi3-64`.
It does not flash a disk. The QEMU profile builds a minimal systemd ARM64 image
with `suifleet` and the SF-030 test updater. The Pi package uses Cortex-A53; the QEMU image uses its standard Cortex-A57
virtual-machine tune. Both are cross-built from the same pinned daemon source.

Preserve the package feed, source/layer revisions, build configuration,
`license.manifest`, image/package manifests, binary/image SHA-256 values and
BitBake logs. `ca-certificates` is an explicit runtime dependency; OpenSSL and
other linked libraries are resolved by Yocto's shared-library packaging/QA.
Use a writable persistent root filesystem for these initial profiles.
Read-only-rootfs/data-partition migration is not implemented here.

## First boot and enrollment

The image contains no private key, device address, deployment manifest,
enrollment config or journal. It installs only `config.toml.example` and
`firmware.toml.example`, with root-controlled policy permissions.

`suifleet-provision.service` runs independently at boot without networking.
It generates `/etc/suifleet/device.key` with the real daemon's `--provision`,
preserves an existing valid key, sets it to `suifleet:suifleet` mode 0600 and
atomically writes the public `/etc/suifleet/device.address`. A corrupt key
causes an explicit failure; it is never silently regenerated. Read the public
address as root or from `journalctl -u suifleet-provision.service`. The helper
prints only the public address. Never run provisioning during image creation.

`suifleet.service` has no privileged pre-start commands. It runs as the
unprivileged account in the existing sandbox and starts only when
`/etc/suifleet/config.toml` exists. `suifleet-enrollment.path` notices that
file's installation; the service also starts on subsequent enrolled boots.

Register the address and obtain the device/capability IDs. Generate a verified
manifest/config bundle using the deployment-manifest workflow, choosing the
updater explicitly. Install `deployment.json` first, root:suifleet mode 0640;
install `config.toml` last and atomically with the same ownership/mode. Paths
must name `/etc/suifleet/device.key` and `/var/lib/suifleet`. Keep enrollment
files outside Git. The shipped example uses `/usr/bin/false`, so an operator
must opt into an actual installed updater. For test images, choose
`/usr/bin/suifleet-test-updater` in direct mode.

Watch `journalctl -u suifleet.service`. Startup verifies deployment metadata;
network failures cause service retry, and established polling retries without
advancing its durable cursor. After changing enrolled configuration, explicitly
restart the daemon. If incorrect config hit a start limit, use
`systemctl reset-failed suifleet.service suifleet-enrollment.path`, then restart
the path/service. Stop the enrollment path before deliberately stopping an
enrolled daemon for maintenance; otherwise PathExists may start it again.
Never delete keys or journals to resolve a network/configuration problem.

The firmware path/service remains separate. It needs an explicitly installed,
root-only `firmware.toml` and a supported privileged updater. No example file
automatically grants firmware authority. SF-030's updater stays unprivileged
and writes only under the daemon state directory.

## Acceptance commands

After building the QEMU profile, point the test at its deployed kernel and
ext4 image and the Yocto native `qemu-system-aarch64`:

```sh
python3 scripts/test_yocto_boot.py \
  --qemu /absolute/path/to/qemu-system-aarch64 \
  --kernel /absolute/path/to/deploy/images/qemuarm64/Image \
  --image /absolute/path/to/deploy/images/qemuarm64/core-image-minimal-qemuarm64.rootfs.ext4
```

This copies the image to disposable disks, disables guest networking, checks
unenrolled provisioning/service state, installs a disposable valid config with
an unavailable loopback node and checks automatic service retry, restarts provisioning, boots the same
disk again to verify identity/state preservation and boots a second fresh copy
to reject cloned keys. It never opens an SD card or modifies the input image.
`scripts/test_yocto_first_boot.py` separately exercises the actual helper and
daemon in fresh containers, including bounded capabilities, unprivileged key
reads, policy permissions and corrupt-key refusal. Container tests do not
substitute for the BitBake build and boot test.

After building the debug daemon in the dev image, run the ordinary CI fixture
with `sh scripts/test_yocto_first_boot.sh sui-dev:latest` (substitute your image
tag). It uses two fresh containers, disables their networks and mounts this
checkout read-only. `.github/workflows/ci.yml` runs it after `make ci` and saves
the output with the other diagnostics.

The pinned OpenSSL 3.5.6 install step is serialized by a layer bbappend. Its
parallel recursive install targets relinked `libcrypto` while modules/programs
were linking during the initial ARM64 build. Compilation remains parallel;
the serialized compile/install task chain passes. The
[upstream parallel-build issue](https://github.com/openssl/openssl/issues/27074)
describes the dependency-race context.

Run the OTA simulator for client reconnect/report/cursor coverage. Actual
enrollment and delivery on the physical Pi, real wallet/testnet/Walrus flows,
and firmware safety remain the separately tracked acceptance work.
