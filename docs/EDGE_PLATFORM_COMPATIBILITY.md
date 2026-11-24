# Edge platform compatibility

Use the [executable profile matrix](../kas/edge-platforms.json) to check the
architecture, libc, kernel, init and Rust toolchain together. The status
`current` points to existing evidence with its own scope. `smoke-only` records
a narrow automated exercise. `candidate` identifies engineering work that
must pass build and runtime checks before any support declaration.

```sh
python3 scripts/edge_platform.py
python3 scripts/edge_platform.py linux-amd64-systemd --require-check ota-simulator
python3 scripts/edge_platform.py linux-amd64-musl-5.15 --require-check legacy-vm-smoke
python3 scripts/edge_platform.py yocto-kirkstone --require-check image-build
```

The last command exits 2 and reports the open work. `--require-check` checks
whether a test is registered for a profile. Run the test itself to obtain
evidence for the machine or guest. Current packaging evidence lives in
[Debian packaging](DEBIAN_PACKAGING.md), [32-bit Linux x86](LINUX_X86.md) and
[Yocto packaging](YOCTO_PACKAGING.md).

## Runtime requirements

| Capability | Constraint and acceptance |
|---|---|
| Isolated signing | Shipped Linux units use separate daemon and signer users, a controlled Unix socket, root-owned MAINPID recording, a sealed cgroup and procfs process inspection. The signer uses Linux 6.5's `SO_PEERPIDFD` to pin the connecting process and fails closed when it cannot authenticate the peer. A BSP claiming a backport needs direct runtime evidence. `pidfd_open` availability alone is insufficient. Portable authentication and independent signing-policy tests belong to [SF-292](https://github.com/tyler-suifleet/suifleet/issues/818). |
| Strict local log ingestion | `logs.rs` uses `openat2` with no-symlink resolution from Linux 5.6. On a stock 4.19/5.4 kernel this path requires a BSP backport or a reviewed equivalent with race and symlink tests. Qualify this constraint on legacy Linux for the [embedded package](https://github.com/tyler-suifleet/suifleet/issues/830) and on Dunfell for the [standalone Yocto layer](https://github.com/tyler-suifleet/suifleet/issues/820). |
| Supervision and restart | Linux uses `prctl(PR_SET_CHILD_SUBREAPER)`, process groups, procfs boot/PID identity and a sealed cgroup v2 for same-boot crash recovery. The unit also supplies privileges, directory ownership, sandboxing, watchdog and process cleanup. The Cargo `systemd` feature controls notification only. OpenRC, SysV and runit service integration and restart tests belong to [SF-293](https://github.com/tyler-suifleet/suifleet/issues/829). |
| Crypto and native dependencies | The locked daemon uses Rustls/ring with trusted CA roots, Ed25519 for software signing and Rust Seal primitives. Target C tooling and compatible headers are required for native crates such as ring and zstd-sys. Optional PKCS#11 needs a compatible provider module and a qualified token/permission setup. A static musl build does not exercise token access or runtime trust-store behavior. |
| Archive codec | The existing archive path uses locked Node 22.22.2 and a digest-bound JS/WASM bundle. The official packaged ARM64 Node requires glibc 2.28 and libstdc++. A musl daemon does not provide a musl-compatible Node package. Exercise the matching Node/codec bundle, CA paths and its memory/CPU budget independently. Embedded packaging and runtime belong to [SF-296](https://github.com/tyler-suifleet/suifleet/issues/830). |
| State and updates | Use persistent writable state and retain the journal across service or platform restarts. Daemon handoff needs a qualified service-manager restart/confirmation path. Platform scripts own updater choice, OS installation, reboot, boot confirmation and firmware rollback. |

## Yocto evaluation

Scarthgap uses the pinned Rust mixin from [the kas profile](../kas/packaging.yml)
and Wrynose uses pinned core Rust in [its kas profile](../kas/packaging-wrynose.yml).
Their respective package/image evidence names the exact source and
configuration. Changing an image, layer pin, BSP or feature set requires
checking the applicable part of that configuration again.

The Kirkstone GA OE-Core tree at `92fcb6570bddd0c5717d8cfdf38ecf3e44942b0f`
ships Rust 1.59.0. The Dunfell GA tree at
`1795f30d8ab73d35710ca99064c51190dc84853e` has no core Rust recipe.
The current layer declares Scarthgap and Wrynose, and its configure step
requires Rust 1.90.0 or newer for the locked dependencies, including
`roaring` 0.11.5. Adding either old
series to the layer declaration would leave these toolchain and recipe
incompatibilities unresolved. The upstream trees document the toolchain baseline in the [Kirkstone Rust recipes](https://github.com/openembedded/openembedded-core/tree/92fcb6570bddd0c5717d8cfdf38ecf3e44942b0f/meta/recipes-devtools/rust)
and [Dunfell development-tool recipes](https://github.com/openembedded/openembedded-core/tree/1795f30d8ab73d35710ca99064c51190dc84853e/meta/recipes-devtools).

The Kirkstone Rust mixin at `6baa9afeb88a4a9b4a0e82dd7e3e611438999dbb`
contains Rust 1.92.0, above the current recipe's compiler floor. Evaluate it
with a pinned compatible Kirkstone point release and matching OE class APIs,
using the existing fixed source and crate inventory, before considering a
Kirkstone layer declaration. The compiler version alone establishes neither
lockfile compatibility nor a successful recipe build.
[The pinned mixin recipe](https://git.yoctoproject.org/meta-lts-mixins/tree/recipes-devtools/rust/rust_1.92.0.bb?id=6baa9afeb88a4a9b4a0e82dd7e3e611438999dbb).

For Dunfell, or if the native Kirkstone path proves incompatible, evaluate a thin
BitBake recipe importing a separately built, authenticated daemon artifact.
Pin the reviewed source revision, Cargo.lock SHA-256, Rust 1.94.0 target and
builder image digest, C compiler/sysroot and artifact SHA-256. Use locked
dependencies and compare two clean builds before pinning the artifact. Start
with AArch64 and validate ELF/ABI, package QA and runtime dependencies against
the selected image. A static musl daemon avoids a GNU loader dependency for
the daemon itself. The separately packaged codec still needs its own
qualification. Use the pinned Yocto release and BSP revisions in separate
build directories, then build the recipe and boot the image with the chosen
kernel. Run signer isolation, service restart and supported feature tests on
that booted image before advertising support. A Rust backport would require
its own pinned Cargo, LLVM, sysroot, OE class and SDK validation. Both
old-series packages remain candidates under the standalone layer work in
[SF-295](https://github.com/tyler-suifleet/suifleet/issues/820).

## Automated checks

The [CI workflow](../.github/workflows/edge-platforms.yml) exercises the
default GNU build with the gRPC OTA simulator on Ubuntu 24.04. Separately it
builds the daemon with `--no-default-features` for
`x86_64-unknown-linux-musl`, boots the checksum-pinned Linux 5.15.0-25-generic
kernel in QEMU and runs the daemon with BusyBox sh as PID 1. The guest checks
disposable identity creation, key mode, address derivation, idempotent
provisioning, offline configuration and identity validation, and rejection of an
unreadable journal. The guest has no NIC, supplies its own kernel and
discards its state after the check. The smoke uses an explicit disposable test key and establishes no
isolated signer, installer, restart, codec or OTA acceptance for Linux 5.15.

Kernels 4.19, 5.4 and 5.10, other legacy architecture/libc/init combinations
and the old Yocto images need their own builds and booted tests. An older
distribution container shares its host kernel. Yocto recipe parsing and a
musl build are each build-time evidence only. Keep physical and manual
real-wallet acceptance in [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32)
and mainnet/production provider acceptance in
[SF-087](https://github.com/tyler-suifleet/suifleet/issues/159).
