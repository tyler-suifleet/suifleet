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
`eedcdc7486ff15c3510c2ceff68c87d9db141312` selects Rust 1.98.1 (the built Cargo reports 1.98.0).
LLVM backends are limited to AArch64 and X86 for this ARM64 target/x86 build host.
Other hosts/architectures need a deliberate profile change and validation.

The local SuiFleet layer comes from this checkout. Archive its commit ID and
any diff alongside the kas configuration and build artifacts. The daemon source
is fetched independently at the recipe's fixed `SRCREV`
`59149f054981b114d09e24224fb54bf720abbe92`, an already merged main revision
containing SF-003/SF-004 gRPC transport and the reviewed generated interfaces.
It requires a node serving indexed gRPC `ListEvents` (Sui ≥ 1.81 in SF-004
tests). Packaging and offline boot checks do not establish real-chain OTA
acceptance. The lockfile SHA-256 is
`ad8c34be35ee73888a43ebee10c3b1c5d95f3b3aaa5e95bd60d9312c9ca9f294`.

`suifleet-crates.inc` declares all 291 registry packages/versions and SHA-256 from
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
with `suifleet`, the SF-030 test updater and `coreutils` for console fixture
transfer. It also builds `qemu-system-native`. The Pi package uses Cortex-A53; the QEMU image uses its standard Cortex-A57
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
# Yocto native QEMU needs the libraries from its recipe native sysroot.
export LD_LIBRARY_PATH=/absolute/path/to/tmp/work/x86_64-linux/qemu-system-native/8.2.7/recipe-sysroot-native/usr/lib
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

Cargo release stripping is disabled in the recipe so Yocto performs symbol
splitting and stripping. C debug/macro prefix mappings also cover Cargo vendor
sources (including ring assembly); package QA is kept enabled.

Run the OTA simulator for client reconnect/report/cursor coverage. Actual
enrollment and delivery on the physical Pi, real wallet/testnet/Walrus flows,
and firmware safety remain the separately tracked acceptance work.


## Recorded target validation (2026-10-14)

The final source is the merged gRPC revision and lock digest listed above.
The actual checked-out layer revisions match `kas/packaging.yml`: Poky
`a53cae3de9f45417b97efc4c46c42e4c8ebdb939`, meta-openembedded
`ae7dfb12245c7f9b9a353499e2688015bd4e6413`, meta-raspberrypi
`2c646d29912dcc873469a57b1c207e1549c5094d` and the pinned Rust mixin.
Actual tools: rustc `1.98.1 (48a229cea 2026-09-01)`, Cargo
`1.98.0 (797e8a9bc 2026-08-05)`, GCC 13.4.0, systemd 255.21,
Linux 6.6.123 and QEMU 8.2.7. The unpacked Git HEAD and Cargo.lock digest
were checked directly. Both compile scripts use `--frozen` and
`CARGO_NET_OFFLINE=true`; all 291 registry crates have checksum fetch entries.

The owned build used the same pinned profile settings, with Pi as the default
machine and a separate `qemu` multiconfig/TMPDIR. Its shared download cache uses
Yocto file locks; outputs and local sstate live outside the checkout. Exact
final build command (exit 0, all 6664 tasks succeeded, 6613 reused):

```sh
source /home/tyler/dev/suifleet-yocto/poky/oe-init-build-env /home/tyler/dev/codex/yocto-sf025/build
bitbake suifleet mc:qemu:core-image-minimal mc:qemu:qemu-system-native > /tmp/sf025-bitbake-ordering.log 2>&1
```

Both package QA tasks passed. The only two warnings concern the local
hash-equivalence server plus sstate-mirror configuration. Earlier actual
failures were corrected: Cargo pre-stripping, ring vendor debug paths,
missing console-transfer utility and a systemd ordering cycle. Path units
start before `basic.target`; they must not wait for the provisioning service
that starts after it. The triggered daemon still requires/orders provisioning.

Exact final boot command (exit 0):

```sh
LD_LIBRARY_PATH=/home/tyler/dev/codex/yocto-sf025/build/tmp-qemu/work/x86_64-linux/qemu-system-native/8.2.7/recipe-sysroot-native/usr/lib \
python3 scripts/test_yocto_boot.py \
  --qemu /home/tyler/dev/codex/yocto-sf025/build/tmp-qemu/sysroots-components/x86_64/qemu-system-native/usr/bin/qemu-system-aarch64 \
  --kernel /home/tyler/dev/codex/yocto-sf025/build/tmp-qemu/deploy/images/qemuarm64/Image \
  --image /home/tyler/dev/codex/yocto-sf025/build/tmp-qemu/deploy/images/qemuarm64/core-image-minimal-qemuarm64.rootfs.ext4 \
  > /tmp/sf025-qemu-boot.log 2>&1
```

All three boots passed. Both fresh disks automatically provisioned independent
keys/addresses without enrollment, and the daemon unit runs as `suifleet`.
Installing a valid disposable config activated the service; unavailable loopback
gRPC produced `currently unavailable: tcp connect error` and a systemd restart.
The test waits for actual restart rather than assuming `network-online.target`
completes within 12 seconds. Rebooting the same disk preserved the exact key
hash, address and filesystem state sentinel, and provisioning restart preserved
identity. The sentinel is not an OTA journal or successful enrollment receipt.
The test uses `-nic none` and copies disks; input-image hashes stayed unchanged.

`file`, `readelf`, package metadata and read-only `debugfs` inspection passed
(`/tmp/sf025-final-artifact-inspection.log`). Both binaries are AArch64 PIEs.
The image binary matches the QEMU package binary. Root owns the executable
and helper (0755), all five units (0644), the config example (root:suifleet
0640), the policy example (root:root 0600) and `/etc/suifleet`
(root:suifleet 0750). The image has no key, address, actual config/manifest,
firmware policy or `/var/lib/suifleet`. Runtime dependencies include
ca-certificates 20260223, OpenSSL 3.5.6, glibc 2.39, libgcc 13.4.0 and
zstd 1.5.5; the manifest also contains the SF-030 test component and coreutils.

| Final artifact | SHA-256 |
|---|---|
| Pi Cortex-A53 packaged daemon | `24b64bd1860b7f62401d87f05122630d8ddeb7b45a2e76b040432db3cff11482` |
| QEMU Cortex-A57 packaged daemon | `77ebc8bfbe2b89457d2ad24222aa7a7c65203ed11117d8b3711fd610d8bb6ff2` |
| QEMU ext4 image | `3985f4808f782ae10031ec8cfda42b645148e0cbe34d94afe2fc272950a1481c` |
| QEMU kernel Image | `a6fabd6fb78a295210511acbf9275ac3198de96436a1a3720dfe3f77f59d2686` |
| QEMU rootfs manifest | `b18d348711af65ff29a40b1be1ccdc8e05eda4e39372380371dc04b23ff33639` |

Current-source regression checks also passed (exit 0):

```sh
docker run --rm -v "$PWD:/workspace" -w /workspace \
  -v suifleet_cargo-registry:/home/dev/.cargo/registry \
  -v suifleet_cargo-git:/home/dev/.cargo/git \
  suifleet-dev:sf004 make edge-test test-ota test-component \
  > /tmp/sf025-grpc-native-checks.log 2>&1
sh scripts/test_yocto_first_boot.sh suifleet-dev:sf004 > /tmp/sf025-grpc-first-boot.log 2>&1
```

That image reports Rust/Cargo 1.94.0 and Sui 1.81.1. All 53 Rust tests,
direct/helper OTA, firmware restart recovery and component OTA checks passed.
Component fixtures ran 14 cases, with five optional BusyBox cases skipped.
Two fresh containers passed independent identities, bounded provisioning
capabilities, unprivileged reads, policy permissions, existing-key preservation
and corrupt-key refusal. Kas 4.7 dump validation, Python syntax and
`git diff --check` also passed.

This validates writable-root emulation and the Pi-machine client package.
A bootable physical Pi image/flash is SF-029; hardware/live-chain/Walrus/wallet
OTA acceptance is SF-031. Read-only roots, separate persistent partitions,
firmware rollback, power-loss safety and physical reboot remain unverified.
