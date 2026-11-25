# Older Yocto LTS series (SF-309)

This page records which pre-Scarthgap Yocto LTS series the SuiFleet layers
support, and what has actually been measured for each. The current series
(Scarthgap and Wrynose) are covered in [Yocto client packaging](YOCTO_PACKAGING.md).
Coordination with the standalone layer work is in SF-295/#820.

`LAYERSERIES_COMPAT` names a series only after a real recipe/image build
from its committed, fully pinned profile. Parsing alone does not count.
Neither does compiling the daemon with a host toolchain. A series whose
runtime still has open kernel or security limits says so here.

## Summary

| Series | Status | Toolchain | Profiles |
|---|---|---|---|
| Kirkstone (4.0) | **Supported build and emulated runtime** (measured 2026-11-24). Strong signer binding not available on its kernels | meta-lts-mixins `kirkstone/rust`, Rust 1.92.0 | `kas/packaging-kirkstone.yml`, `kas/packaging-kirkstone-qemu.yml` |
| Dunfell (3.1) | **Blocked**: no Rust ≥ 1.90 toolchain; not advertised | none available | none |

## Kirkstone

### Pinned inputs

| Repository | Revision | Note |
|---|---|---|
| poky | `445a6223929b9a7a62b575093659e3d9e1aba982` | `yocto-4.0.35-4-g445a622392`, kirkstone branch, 2026-07-31 |
| meta-openembedded | `ce8539c941f6fcbecaca4d16640ac105c0595589` | kirkstone branch, 2026-05-04 |
| meta-raspberrypi | `255500dd9f6a01a3445ac491d1abc401801e3bad` | kirkstone branch |
| meta-lts-mixins | `6baa9afeb88a4a9b4a0e82dd7e3e611438999dbb` | `kirkstone/rust`, Rust/Cargo 1.92.0, 2026-01-25 |

Core Kirkstone ships Rust 1.59, which cannot build this lockfile. The recipe
requires Rust 1.90 or later and checks the staged compiler before configure.
The Kirkstone Rust mixin is a different layer from the Scarthgap mixin. It
masks core's rust/cargo recipes and supplies the
`cargo-update-recipe-crates` class (core Kirkstone predates it) and the
`Unicode-3.0` license text the codec recipe declares. The profile does not
reuse the Scarthgap mixin.

Other component versions in this poky revision: GCC 11.5, glibc 2.35,
systemd 250.14, linux-yocto 5.15 (qemuarm64 default) and QEMU 6.2.0. The prebuilt
Node 22.22.2 needs glibc ≥ 2.28, which glibc 2.35 meets. Every directive
in the SuiFleet units exists in systemd 250. The newest are `ProtectProc` and
`ProcSubset` (247) and `ProtectClock` (245). The `--extended` boot check
below rejects any systemd parse diagnostic for those units. That matters
because systemd ignores an unknown directive, which would silently weaken the
sandbox.

### Metadata compatibility

The layers use only syntax BitBake 2.0 accepts: `:append`/`:remove`
overrides, `LICENSE_FLAGS_ACCEPTED` and `UNPACKDIR` fallback to `WORKDIR`.
Recipe metadata did not need any Kirkstone-specific change. Parsing all
recipes of the QEMU profile succeeded with the current daemon pin (BitBake
2.0, 1329 setscene tasks scheduled). That shows parse compatibility only.

### Build and runtime evidence

All results below are from 2026-11-24, at daemon pin
`b9bb2ef6210624d732ab2de2075ea1826e5300c1` (client 0.2.3, lock SHA-256
`c2dcebe1…a789de6`). The host was Ubuntu 22.04 (Python 3.10, GCC 11.4), kas
4.7 with the committed profiles, and both SuiFleet layers from this checkout.
A local `site.conf` set the download, sstate and TMPDIR locations, `rm_work`,
the codec artifact directory, a disk monitor and higher task concurrency.
It changed no source pins.

**Clean QEMU profile build** (`kas build kas/packaging-kirkstone-qemu.yml`):
exit 0, all 3557 tasks succeeded. Every task was built from source on this
host. An attempt to use the public Yocto sstate mirror matched 0 objects and
was dropped. Earlier attempts of the same build were interrupted only by host
disk exhaustion; their locally built sstate supplied 1834 of the final run's
tasks. The final run warned only about two upstream fetch fallbacks (bash and
xmlto tarballs fetched from mirrors). For the `suifleet` recipe:

- `do_verify_lock` matched the lock digest and crate version.
- `do_verify_toolchain` logged
  `rustc 1.92.0 (ded5c06cf 2025-12-08) (built from a source tarball)`.
- `do_compile` ran `cargo build --frozen` offline.
- `do_package_qa` passed with no QA issues.

The codec recipe's `do_verify_codec` accepted the five pinned artifacts. The
Node and test-component recipes also passed package QA.

| Artifact | SHA-256 |
|---|---|
| QEMU Cortex-A57 packaged daemon (`/usr/bin/suifleet`, AArch64 PIE) | `24d3f1ee5bce5bdb0a1ac3ce600fa8e2bcf4fb973c40b9dc529c4da4ed813b60` |
| QEMU ext4 image | `6add59ed01eb8a4bd8c899d9ee0b34c8b5ac4a0b3d1674f32967ba66b16c7b7a` |
| QEMU kernel Image (linux-yocto 5.15.201) | `7956bc25737cd6e81188746c9f238840efd13c1663b1cdddfc54e9a7a4cfd823` |
| QEMU rootfs manifest | `b9d3ebea643ec33ef124bcd2ebad3f12aa979857210ce7ef1a2954defa72b7f7` |

The image manifest contains systemd 250.14, glibc 2.35, libstdc++ 11.5,
busybox 1.35, shadow 4.11.1, ca-certificates 20211016, suifleet-node 22.22.2,
suifleet-walrus-codec 1.0 and the test component. The image daemon equals
the packaged binary. Its dynamic dependencies are libgcc_s, libm, libc and
the loader, with no libssl or libcrypto. Read-only `debugfs` inspection
found:

- `/etc/suifleet` is root:suifleet 0750.
- `config.toml.example` is root:suifleet 0640, `firmware.toml.example` is
  root 0600 and `signer.toml.example` is root:suifleet-signer 0640.
- The executable and the provisioning helper are root 0755.
- There is no key, address, config, manifest or `/var/lib/suifleet`.
- All five codec files match the recipe pins.

Kirkstone's ca-certificates bundle dates from 2021-10. Check it against your
endpoints' roots before deploying.

**QEMU runtime** (`scripts/test_yocto_boot.py --extended`, Yocto
qemu-system-aarch64 6.2.0): exit 0 across three boots, with guest kernel
`5.15.201-yocto-standard` and systemd 250 (250.14). The input image hash
was unchanged afterwards. The run checked:

- Unenrolled automatic provisioning and an unprivileged daemon unit.
- Config activation, then retry against an unavailable loopback node.
- No systemd parse diagnostics for SuiFleet units. The boot journal capture
  must succeed and contain the daemon's own output, and only grep's clean
  no-match status passes.
- The packaged Node (required in this profile) reports v22.22.2, equal to the
  codec manifest, and runs.
- The test updater installs v1, refuses a malformed artifact with v1 still
  current, and upgrades to v2 with a receipt naming the previous version.
  It runs as the daemon user.
- After switching to the service signer, the unchanged key is
  suifleet-signer 0600. The daemon user cannot read it. The socket is
  suifleet-signer:suifleet 0660. With the service stopped, a connection to
  the socket from the daemon user activated it. It answered the device
  address, refused a signature request from a process that is not the
  recorded daemon main process, and refused an unknown operation. This ran on
  the 5.15 kernel, so the refusal used the /proc caller fallback without
  pidfd confirmation (SF-292).
- Rebooting the same disk preserves the key, address and state.
- A second fresh copy provisions a different identity.

**Exported software layer** (`scripts/export_yocto_layer.py` at the commit
that declares Kirkstone): `yocto-check-layer` against Kirkstone core plus the
Kirkstone Rust mixin, with `--dependency meta-oe --no-auto-dependency` and
without the integration layer, returned exit 0, `PASS`, 7 tests and 2
BSP/distro skips. It covered layerseries compat, parse, README, environment,
signatures and both world checks. As on the newer series, this is not a
standalone recipe task-execution build (SF-308).

**Pi package profile** (`kas build kas/packaging-kirkstone.yml`,
`raspberrypi3-64`, Cortex-A53 tune): exit 0. The final run attempted all 3385
tasks and all succeeded, 3056 of them reused from the local sstate. The
toolchain gate reported the same rustc 1.92.0, and `suifleet` package QA
passed. The packaged daemon is an AArch64 PIE, SHA-256
`310797987da766d58523efecc934f0c1ce941bbdd41b62f0a24abfb010a87215`, and its
`build-info` names the pinned source revision.

Two earlier attempts each failed in an unrelated recipe's Meson configure
(wayland, then python3-pycairo) with `Clock skew detected` (1.2 and 1.7
seconds in the future). That is the same WSL timestamp jitter SF-025
recorded. Each retry passed unchanged, and no clock or QA check was
disabled. This is a package build, not a Pi image, flash or boot (SF-029,
SF-031).

Not shown by any of these checks: a successful signature (no transaction
was signed and no chain was reached), real enrollment or OTA, or behaviour
under the 6.5+ pidfd path.

### Kernel and signer boundary (SF-292)

The isolated signing service pins its caller with `SO_PEERPIDFD` (Linux 6.5+;
see [key isolation](KEY_ISOLATION.md)). Kirkstone's kernels (linux-yocto 5.10
and 5.15) do not provide it. `peer_pidfd` then returns nothing. The caller
check still compares the peer's PID and start time with the recorded main
process and checks its cgroup. It no longer confirms, after reading `/proc`,
that the connected process is that same live process. A booted Kirkstone
image with its own kernel therefore runs the weaker binding. It does not
have the strong signer guarantees documented for 6.5+ kernels.

SF-309 does not advertise strong-signer runtime support on old kernels. The
portable race-resistant peer binding, and the decision on whether the
fallback is acceptable, belong to SF-292. Old userspace on a 6.5+ kernel
(for example a Kirkstone rootfs with a newer BSP kernel) is a different
configuration and has not been measured here.

## Dunfell

Dunfell (3.1, final release yocto-3.1.33) is not supported and has no
profile. The blocker is the toolchain, and it is outside SuiFleet's control:

- meta-lts-mixins has no `dunfell/rust` branch. It has `dunfell/go`,
  `dunfell/docker` and `dunfell/kernel`.
- meta-rust's `dunfell` branch ends at Rust 1.72.0 (`d42984ff`, 2023-09-17).
  meta-rust `master` reaches Rust 1.94.1, but its `LAYERSERIES_COMPAT`
  starts at gatesgarth.
- The locked dependency graph needs Rust ≥ 1.90 (edition 2024 crates), and
  the recipe rejects older compilers.

Supporting Dunfell would mean porting and maintaining a Rust ≥ 1.90 toolchain
on Dunfell's BitBake 1.46 and GCC 9. Its kernels (5.4) also lack
`SO_PEERPIDFD`, the same signer limit as Kirkstone. Neither has been
attempted. Integrators on Dunfell can still deploy a separately built client
binary under their own validation, but that is not layer support.

## Reproduce

```sh
export KAS_WORK_DIR=/absolute/path/to/yocto-kirkstone
export KAS_BUILD_DIR="$KAS_WORK_DIR/build-qemu"
kas build kas/packaging-kirkstone-qemu.yml
export KAS_BUILD_DIR="$KAS_WORK_DIR/build-pi"
kas build kas/packaging-kirkstone.yml
```

Keep each series in its own work tree; do not mix release TMPDIRs. A local
`conf/site.conf` may set the download/sstate directories, `rm_work` and the
codec artifact directory without changing source pins. The QEMU boot check
uses the QEMU build directory, not the Pi one exported last above. Set
`TMP` to that build's `TMPDIR` (`$KAS_BUILD_DIR/tmp` unless `site.conf`
moves it). The native `qemu-system-aarch64` (QEMU 6.2.0) links
`libfdt.so.1`, which comes from `dtc-native` and is not on the host's default
library path. Without `LD_LIBRARY_PATH` it fails to start in a clean shell:

```sh
export KAS_BUILD_DIR="$KAS_WORK_DIR/build-qemu"
TMP="$KAS_BUILD_DIR/tmp"
export LD_LIBRARY_PATH="$TMP/sysroots-components/x86_64/dtc-native/usr/lib"
"$TMP/sysroots-components/x86_64/qemu-system-native/usr/bin/qemu-system-aarch64" --version
python3 scripts/test_yocto_boot.py --extended \
  --qemu "$TMP/sysroots-components/x86_64/qemu-system-native/usr/bin/qemu-system-aarch64" \
  --kernel "$TMP/deploy/images/qemuarm64/Image" \
  --image "$TMP/deploy/images/qemuarm64/core-image-minimal-qemuarm64.ext4"
```

`--extended` also records the guest kernel and systemd versions. It rejects
systemd parse diagnostics for SuiFleet units. It applies, refuses and upgrades
the harmless SF-030 test component as the daemon user. It switches the device
to the isolated signer and checks that the unchanged key moves to
`suifleet-signer` and the daemon user cannot read it. With the service
stopped, a local connection to the socket (from the packaged Node runtime, as
the daemon user) must activate it. The service must answer the device address
and refuse two requests: a signature from a process that is not the recorded
daemon main process, and an unknown operation. It signs no transaction and
does not reach a chain.

## Not covered

- Physical Raspberry Pi boot of a Kirkstone image (SF-031).
- Real-chain enrollment or OTA on any older series.
- Read-only roots and non-systemd init (SF-293).
- A recipe build of the standalone layer without `meta-suifleet-integration`
  (SF-308). The Kirkstone builds above included it. Its unconditional
  OpenSSL install serialization (applied to Kirkstone's OpenSSL 3.0.19) and
  AArch64 glibc usrmerge guard were active. The Wrynose-only libcap/xz
  appends were inactive.
