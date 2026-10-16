# Yocto client packaging

SF-025 packages the daemon and first-boot/enrollment services. SF-029 supplies
the physical Raspberry Pi image/test deployment workflow; SF-031 owns live OTA
acceptance. A boot test in QEMU is not evidence of Pi firmware, power-loss or
public-network acceptance.

## Pinned build inputs

The Scarthgap profile uses the user's selected `meta-lts-mixins` `scarthgap/rust`
layer. Plain Scarthgap Rust 1.75 cannot compile this lockfile. The committed
[kas packaging profile](../kas/packaging.yml) pins Poky, meta-openembedded,
meta-raspberrypi and the Rust mixin to full Git revisions. The mixin revision
`eedcdc7486ff15c3510c2ceff68c87d9db141312` selects Rust 1.98.1 (the built Cargo reports 1.98.0).
LLVM backends are limited to AArch64 and X86 for this ARM64 target/x86 build host.
Other hosts/architectures need a deliberate profile change and validation.

The local SuiFleet layer comes from this checkout. Archive its commit ID and
any diff alongside the kas configuration and build artifacts. The daemon source
is fetched independently at the recipe's fixed `SRCREV`
`795de747e066ee97af0e9e0e53deda3c6c71e90c`, reviewed main including the
integrated gRPC, identity, catalog/dependency, desired-state and deployment-control
implementations. `nobranch=1` selects this exact immutable revision, never AUTOREV.
The daemon crate version is `0.2.1`. It requires indexed gRPC `ListEvents`
(Sui ≥ 1.81 in SF-004 tests). Packaging/offline checks do not establish
real-chain OTA acceptance. The Cargo.lock SHA-256 is
`44aa5b2637fc2b828a796a63e9a550d9c0fa652437cf59ffd31e0b264021290e`.

`suifleet-crates.inc` declares all 302 registry packages/versions and SHA-256 from
that revision's Cargo.lock. BitBake fetches/unpacks them before compilation;
Yocto's cargo class vendors them locally and passes `--frozen`. The recipe also
sets `CARGO_NET_OFFLINE=true` and checks the lockfile digest before configure.
There is no compile-time crate download or `AUTOREV`.

The bootable SD image profile, access provisioning and flashing instructions are
in [PI_IMAGE.md](PI_IMAGE.md). Historical validation below remains tied to its
recorded older sources; use the SF-029 image evidence for the new pin.

To update: select a reviewed source revision, update `SRCREV`, run
`bitbake -c update_crates suifleet`, and update `SUIFLEET_LOCK_SHA256` from that
revision's `suifleet-edge/Cargo.lock`. Review the generated include and build
the package/image again. Yocto documents this workflow in its
[Cargo classes reference](https://docs.yoctoproject.org/5.0.17/ref-manual/classes.html#cargo-update-recipe-crates).
Do not merely disable the digest check to accommodate a lockfile change.

The Wrynose profiles are [packaging-wrynose.yml](../kas/packaging-wrynose.yml)
and [packaging-wrynose-qemu.yml](../kas/packaging-wrynose-qemu.yml). They pin
OE-Core, BitBake 2.18, meta-openembedded and meta-raspberrypi directly and use
`nodistro` with systemd and core Rust 1.94.1, without the Scarthgap Rust mixin.
Both series have the package/image and emulated boot validation recorded below.
Support here covers the ARM64 Pi client package and writable-root QEMU image;
physical Pi image/boot acceptance remains SF-029/SF-031. See the [Wrynose migration guide](https://docs.yoctoproject.org/6.0/migration-guides/migration-6.0.html).

Both recipe variants preserve core's `UNPACKDIR` when present and fall back to
`WORKDIR` only for Scarthgap. Git explicitly unpacks to `git` on both releases;
local install inputs use `UNPACKDIR`. Before configure, the recipe executes the
staged compiler's `--version` and rejects Rust below 1.90.0. The mixin dependency
belongs to the Scarthgap kas profile, rather than the reusable layer.

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

# Wrynose uses its own workspace/build trees (do not mix release TMPDIRs).
export KAS_WORK_DIR=/absolute/path/to/yocto-wrynose
export KAS_BUILD_DIR="$KAS_WORK_DIR/build-pi"
kas build kas/packaging-wrynose.yml
export KAS_BUILD_DIR="$KAS_WORK_DIR/build-qemu"
kas build kas/packaging-wrynose-qemu.yml
```

The base profile cross-builds the `suifleet` package for `raspberrypi3-64`.
It does not flash a disk. The QEMU profile builds a minimal systemd ARM64 image
with `suifleet`, the SF-030 test updater and `coreutils` for console fixture
transfer. It also builds `qemu-system-native`. The Wrynose emulator omits SDL
and audio because the acceptance runner uses only serial/disk emulation. The Pi package uses Cortex-A53; the QEMU image uses its standard Cortex-A57
virtual-machine tune. Both are cross-built from the same pinned daemon source.

Preserve the package feed, source/layer revisions, build configuration,
`license.manifest`, image/package manifests, binary/image SHA-256 values and
BitBake logs. `ca-certificates` is an explicit runtime dependency; the daemon uses Rustls and has no direct OpenSSL build dependency. Systemd and
other image/native dependencies still use OpenSSL, so the observed recursive
install serialization workaround remains in the layer. Shared-library QA stays
enabled.
Use a writable persistent root filesystem for these initial profiles.
Read-only-rootfs/data-partition migration is not implemented here.

## First boot and enrollment

The image contains no private key, device address, deployment manifest,
enrollment config or journal. It installs only `config.toml.example` and
`firmware.toml.example`, with root-controlled policy permissions.

`suifleet-provision.service` runs independently at boot without networking.
The real daemon's `--provision` writes the key to a private mode-0600 staging
file, synchronizes its complete contents,
publishes it using an atomic no-overwrite hard link, and synchronizes the parent
directory. Interrupted staging bytes are never loaded as the device identity;
a competing creator's published key is preserved. It preserves an existing
valid key, sets it to `suifleet:suifleet` mode 0600, and atomically writes the public `/etc/suifleet/device.address`. A corrupt key
causes an explicit failure; it is never silently regenerated. Read the public
address as root or from `journalctl -u suifleet-provision.service`. The helper
prints only the public address. Never run provisioning during image creation.

`suifleet.service` has no privileged pre-start commands. It runs as the
unprivileged account in the existing sandbox and starts only when
`/etc/suifleet/config.toml` exists. `suifleet-enrollment.path` notices that
file's installation; the service also starts on subsequent enrolled boots.

Register the address through the current registry flow and obtain the
device/capability IDs. Existing fleets must complete the registry-identity
activation and receipt migration described in [the operational runbook](OPERATIONS.md#device-identity-lifecycle-sf-008)
before switching to this client. Provisioning preserves key bytes; it does not
activate on-chain identities or migrate objects. Generate a verified
manifest/config bundle using the deployment-manifest workflow, choosing the
updater explicitly. Install `deployment.json` first, root:suifleet mode 0640;
install `config.toml` last and atomically with the same ownership/mode. Paths
must name `/etc/suifleet/device.key` and `/var/lib/suifleet`. Keep enrollment
files outside Git. The shipped example uses `/usr/bin/false`, so an operator
must opt into an actual installed updater. For test images, choose
`/usr/bin/suifleet-test-updater` in direct mode. The pinned client also includes
`--verify-release`; provision its public trust policy through the
[signed-release workflow](RELEASES.md) when using that integration. Device
production trust acceptance remains SF-042; the image does not provision it.

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


<a id="recorded-target-validation-2026-10-14"></a>

## Historical target validation (2026-10-14, before atomic-key follow-up)

This section records source `59149f054981b114d09e24224fb54bf720abbe92`,
not the newer atomic-key source pin above. Its artifact hashes and boot results
must not be attributed to the new source or the newly rebuilt images.
Its lock digest was `ad8c34be35ee73888a43ebee10c3b1c5d95f3b3aaa5e95bd60d9312c9ca9f294`;
it is not the final Rustls lock digest above.
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

## PR feedback follow-up (2026-10-14)

The first-boot CI step explicitly selects Bash so a failing test before `tee`
fails the step. The QEMU runner explicitly rejects an active unenrolled daemon
and a nonzero restart count; `--console-timeout` controls console/login/command
waits (default 300 seconds). Provisioning signal handlers exit through the
cleanup trap. The OpenSSL install serialization append now matches point
releases; this does not establish compatibility with a different OpenSSL major.

The follow-up adds the Wrynose profiles, portable unpack layout and
actual compiler-version gate. Atomic no-overwrite key publication now has a
failure-path regression covering partial-write failure, retry with abandoned
staging bytes, unchanged existing keys, permissions and competing publication.
The address helper installs cleanup/signal traps before `mktemp`, guarding an
empty temporary name. Hardware power-loss safety is still unverified.

The final source, commands and tested artifacts are recorded in the next section.
Intermediate runs of source `57abcd1` are superseded by the integrated source
`56bbac4`; their image hashes are not the final acceptance artifacts.

<a id="atomic-key-follow-up-validation-2026-10-14"></a>

## Final LTS validation (2026-10-15 UTC)

The source pin is `56bbac4d3286f32cad954ad8679bf0d4acf1b838`, containing the
atomic identity change and reviewed main through `38beecd`. Actual BitBake
`update_crates` regenerated 285 checksum fetch entries; the lock digest is
`31360ca31fc20fcfd66b0a370fc5bfd31693a1c3e036d69720cdef368f0be822`.
All four unpacked source HEADs and lock digests were checked directly.
The daemon source/Cargo files in the feedback branch match that pin exactly.

| Profile | Verified tools | Result |
|---|---|---|
| Scarthgap/Rust mixin | rustc 1.98.1, Cargo 1.98.0, GCC 13.4.0, systemd 255.21, Linux 6.6.123, QEMU 8.2.7 | 6666 tasks succeeded, 6611 reused; both daemon package QA tasks passed |
| Wrynose | rustc 1.94.1, Cargo 1.94.0, GCC 15.3.0, systemd 259.5, Linux 6.18.52, QEMU 10.2.0 | 6848 tasks succeeded, 6790 reused; both daemon package QA tasks passed |

Both builds returned exit 0. Scarthgap emitted the two local mirror/hash-server
warnings and a retained forced-task warning from the compiler-gate negative
fixture; Wrynose's final build had no warnings. The gate checks the staged
compiler, including the explicit native-path fallback needed when Wrynose's
Cargo class leaves `RUSTC` unset. A fixture reporting Rust 1.89.0 was rejected
with `require Rust >= 1.90.0`; the normal compilers passed. Compile remains
frozen and offline.

The actual layer revisions match the committed kas profiles. The Scarthgap
command used the existing Pi default plus QEMU multiconfig. Wrynose used the
kas-generated QEMU config plus `mc:pi`, a separate `tmp-pi`, shared downloads,
and local sstate. Its local concurrent-task limit was raised to four; compiler
jobs remained `-j4`. These cache/concurrency settings change no source pins.
Exact build commands (after `umask 022`):

```sh
source /home/tyler/dev/suifleet-yocto/poky/oe-init-build-env /home/tyler/dev/codex/yocto-sf025/build
bitbake suifleet mc:qemu:core-image-minimal mc:qemu:qemu-system-native

source /home/tyler/dev/codex/yocto-sf025-wrynose/openembedded-core/oe-init-build-env /home/tyler/dev/codex/yocto-sf025-wrynose/build-qemu
bitbake -k core-image-minimal qemu-system-native mc:pi:suifleet
```

The failed attempts were retained and corrected: the initial delayed unpack
fallback incorrectly enlarged Scarthgap's pseudo ignore paths; Wrynose libcap
2.77 relinked during install using host GCC; generated `xzmore` needed a declared
Bash dependency and usrmerge interpreter path. The Wrynose-only appends preserve
Scarthgap metadata. Scarthgap xz regression QA passed (1017 tasks, 985 reused).
A transient Meson future timestamp (1.3055 seconds) passed on retry with clock
checks enabled. Changes made during an earlier build caused stale task hashes;
all final runs used stable metadata and returned success. The optional public
hash-equivalence endpoint timed out and was removed from the local config.
No package QA or clock check was disabled.

Exact final boot commands:

```sh
LD_LIBRARY_PATH=/home/tyler/dev/codex/yocto-sf025/build/tmp-qemu/work/x86_64-linux/qemu-system-native/8.2.7/recipe-sysroot-native/usr/lib \
python3 scripts/test_yocto_boot.py \
  --qemu /home/tyler/dev/codex/yocto-sf025/build/tmp-qemu/sysroots-components/x86_64/qemu-system-native/usr/bin/qemu-system-aarch64 \
  --kernel /home/tyler/dev/codex/yocto-sf025/build/tmp-qemu/deploy/images/qemuarm64/Image \
  --image /home/tyler/dev/codex/yocto-sf025/build/tmp-qemu/deploy/images/qemuarm64/core-image-minimal-qemuarm64.rootfs.ext4

LD_LIBRARY_PATH=/home/tyler/dev/codex/yocto-sf025-wrynose/build-qemu/tmp/work/x86_64-linux/qemu-system-native/10.2.0/recipe-sysroot-native/usr/lib \
python3 scripts/test_yocto_boot.py \
  --qemu /home/tyler/dev/codex/yocto-sf025-wrynose/build-qemu/tmp/sysroots-components/x86_64/qemu-system-native/usr/bin/qemu-system-aarch64 \
  --kernel /home/tyler/dev/codex/yocto-sf025-wrynose/build-qemu/tmp/deploy/images/qemuarm64/Image \
  --image /home/tyler/dev/codex/yocto-sf025-wrynose/build-qemu/tmp/deploy/images/qemuarm64/core-image-minimal-qemuarm64.rootfs.ext4
```

Both returned exit 0, three boots each. Each image provisions distinct fresh
identities while unenrolled, exposes an unprivileged daemon unit, activates on
config installation, retries the unavailable loopback node, preserves exact
identity and a filesystem state sentinel across reboot, and preserves identity
on provisioning restart. Both final input image hashes remained unchanged.
The sentinel is not a successful OTA receipt or real-chain enrollment record.

Read-only debugfs inspection found no baked private key, address, actual
enrollment/config/policy files or live state. Executable/helper modes are 0755,
`/etc/suifleet` is root:suifleet 0750, config example root:suifleet 0640, and policy
example root:root 0600. Both image daemons match their packaged binaries. All
four binaries are AArch64 PIEs, have no dynamic libssl/libcrypto dependency,
and match the separate debug-package CRCs. The recipe retains CA certificates.

| Final artifact | Scarthgap SHA-256 | Wrynose SHA-256 |
|---|---|---|
| Pi packaged daemon | `73b827bfbdcf05e958e35a6514d2ea205b8377df5309ac2b446cf8511c87d4eb` | `972a71ee17baefb7e20fff8bed60308a5246148e4fbd2799f873755d3ba7b222` |
| QEMU packaged daemon | `8d6fbd39cb2c6b02d64b94b6283a74cfb6a97d4e6a4c77526beb50aec0420ee7` | `17b8dfa1a741d74419069251d4ebc4e613a6fdf9aaaf646ae4f5c7527b27f0a5` |
| QEMU ext4 image | `944164abc99b4df128fb0495961881521964ef9c6cb0dd0be6cc399fed53de0c` | `2d1a35086ea18bb228480cf626a23b5ebb767afb94f5dff41c7599ef5606c694` |
| QEMU kernel | `a6fabd6fb78a295210511acbf9275ac3198de96436a1a3720dfe3f77f59d2686` | `551072885a666afbfc11727372e289c066db6cdf593e5217bab6ff05fc2a3ecf` |
| QEMU rootfs manifest | `b18d348711af65ff29a40b1be1ccdc8e05eda4e39372380371dc04b23ff33639` | `8376e4f1adac69e3964a1e8c9f7bb7dda1f1cbb51962e1aaf0920091d3bef4f9` |

The final local suite uses the pinned `suifleet-dev:sf004` image (Rust/Cargo
1.94.0, Sui 1.81.1). It covers 37 Move tests, 62 Rust tests, reviewed/generated
interfaces, publisher/manifest/testnet-controller offline tests, direct/helper
OTA, firmware restart recovery, component v1/v2/install/health failure cases,
frontend typecheck and production build. Component fixtures ran 14 cases with
five optional BusyBox cases skipped. Fresh-container provisioning checks passed
independent identities, permissions, bounded capabilities, stable restart and
corrupt-key refusal. The interrupted-write identity test also covers abandoned
staging bytes, retry, unchanged existing keys and competing publication.

A linked worktree needs its common Git directory mounted for the aggregate
suite's final `git diff --check`. The first aggregate run passed every application
check but ended at that unmapped Git path; the check was rerun successfully on
the host and with the metadata mounted. The corrected aggregate suite and fresh-container checks returned exit 0.
The corrected full-suite command is:

```sh
repository_git_common=$(git rev-parse --path-format=absolute --git-common-dir)
docker run --rm -v "$PWD:/workspace" \
  -v "$repository_git_common:$repository_git_common:ro" -w /workspace \
  -v suifleet_cargo-registry:/home/dev/.cargo/registry \
  -v suifleet_cargo-git:/home/dev/.cargo/git \
  suifleet-dev:sf004 make check
sh scripts/test_yocto_first_boot.sh suifleet-dev:sf004
```

Public-chain transactions, physical Pi programming and production signing-key
provisioning were not performed by this worker. Physical image/OTA acceptance
remains SF-029/SF-031 and provisioned release trust remains SF-042. Read-only
roots, separate persistent partitions and physical power-loss/rollback safety
remain unverified. Current-head CI and independent review are separate merge
gates; automatic testnet status, if failing, must remain visible on the PR.
