# Edge platform compatibility

Use the [executable profile matrix](../kas/edge-platforms.json) to check the
architecture, libc, kernel, init and Rust toolchain together. The status
`current` points to existing evidence with its own scope. `smoke-only` records
a narrow automated exercise. `candidate` identifies engineering work that
must pass build and runtime checks before any support declaration. `blocked`
records an evaluated profile with an unresolved prerequisite.

```sh
python3 scripts/edge_platform.py
python3 scripts/edge_platform.py linux-amd64-systemd --require-check ota-simulator
python3 scripts/edge_platform.py linux-amd64-musl-5.15 --require-check legacy-vm-smoke
python3 scripts/edge_platform.py yocto-kirkstone --require-check image-build
python3 scripts/edge_platform.py yocto-dunfell --require-check image-build
```

The last command exits 2 and reports the open work. `--require-check` checks
whether a check is registered for a profile. Run the test itself to obtain
evidence for the machine or guest. Current packaging evidence lives in
[Debian packaging](DEBIAN_PACKAGING.md), [32-bit Linux x86](LINUX_X86.md) and
[Yocto packaging](YOCTO_PACKAGING.md).

## Runtime requirements

| Capability | Constraint and acceptance |
|---|---|
| Isolated signing | Shipped Linux units use separate daemon and signer users, a controlled Unix socket, root-owned MAINPID recording, a sealed cgroup and procfs process inspection. The signer uses Linux 6.5's `SO_PEERPIDFD` to pin the connecting process and fails closed when it cannot authenticate the peer. A BSP claiming a backport needs direct runtime evidence. `pidfd_open` availability alone is insufficient. The [portable Linux signer](KEY_ISOLATION.md#portable-linux-signer) uses a trusted launcher, kernel peer credentials and a pinned procfs task where systemd or `SO_PEERPIDFD` is unavailable. It requires Python 3.9+, suitable procfs access and `fs.suid_dumpable=0`. Qualify the launcher and restart behavior on the selected image and init system. |
| Strict local log ingestion | `logs.rs` uses `openat2` with no-symlink resolution from Linux 5.6. On a stock 4.19/5.4 kernel this path requires a BSP backport or a reviewed equivalent with race and symlink tests. Qualify this constraint on legacy Linux for the [embedded package](https://github.com/tyler-suifleet/suifleet/issues/830) and for any separately qualified Dunfell integration under the [standalone Yocto layer](https://github.com/tyler-suifleet/suifleet/issues/820). |
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

The layer also declares Kirkstone. Its pinned `kirkstone/rust` mixin at
`6baa9afeb88a4a9b4a0e82dd7e3e611438999dbb` provides Rust 1.92.0 and
the Cargo recipe support needed by the locked dependencies. [Legacy Yocto
evidence](YOCTO_LEGACY_SERIES.md#kirkstone) records the Kirkstone AArch64
QEMU image build and systemd smoke on Linux 5.15, plus the Raspberry Pi
package build. Run `kas build kas/packaging-kirkstone-qemu.yml` for the
registered image build. The Kirkstone 5.10 and 5.15 kernels lack
`SO_PEERPIDFD`, so the shipped systemd signer cannot provide strong process
binding on those kernels. Portable signer integration on the Kirkstone image
still needs validation. Physical Pi image boot and runtime acceptance remain
in [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32).

[Dunfell is blocked](YOCTO_LEGACY_SERIES.md#dunfell). Its available toolchains
fall below the recipe's Rust 1.90 floor, and the layer has no Dunfell kas
profile or compatibility declaration. Integrators can evaluate a separately
built client against their exact target. Pin the reviewed source, Cargo.lock
digest, builder, Rust target, C toolchain, sysroot and artifact digest. Check
ELF/ABI, package QA, codec dependencies, signer integration and restart
behavior on the booted image. Standalone layer publication is tracked in
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

Kernels 4.19, 5.4 and 5.10 and other legacy architecture/libc/init
combinations need specific builds and booted tests. An older
distribution container shares its host kernel. Yocto recipe parsing and a
musl build are each build-time evidence only. Keep physical and manual
real-wallet acceptance in [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32)
and mainnet/production provider acceptance in
[SF-087](https://github.com/tyler-suifleet/suifleet/issues/159).
