# 32-bit Linux x86 runtime (SF-076)

SuiFleet builds, packages and selects components for 32-bit Linux x86. Its
real daemon is tested in a 32-bit userland. This page states the supported
ABI, how to build and package for it, how components select it, and what the
evidence covers. It establishes the daemon on that ABI. It does not establish
a 32-bit kernel, physical i686 hardware, a production OS image or Greengrass
parity. An x86_64 test result says nothing about 32-bit behaviour, so every
claim below comes from the 32-bit build.

## Supported ABI and toolchain

| Item | Supported |
|---|---|
| Rust target | `i686-unknown-linux-gnu`, Rust 1.94.0 (the pinned baseline) |
| CPU | Pentium 4 class or later (the target's SSE2 baseline). `i586`/no-SSE2 CPUs are not supported |
| C library | glibc. The binary needs symbols up to **GLIBC_2.34**. It links against the cross glibc 2.35 and is tested on Debian 12 glibc 2.36. musl is not supported |
| Loader / ELF | `/lib/ld-linux.so.2`, ELF32 little-endian, `EM_386` |
| Kernel | Linux 3.2 or later (the Rust target minimum). Tested only through an x86_64 host kernel's IA-32 compatibility layer (WSL2 6.6) |
| Catalog platform | `linux` / `x86`, Rust's `std::env::consts::ARCH`. `i686` and `i386` are not aliases and are rejected |
| Cross toolchain | Ubuntu 22.04 `gcc-i686-linux-gnu` 4:11.2.0-1ubuntu1 (gcc 11.4.0-1ubuntu1~22.04.3cross1), `binutils-i686-linux-gnu` 2.38-4ubuntu2.12 and `libc6-dev-i386-cross` 2.35-0ubuntu1cross3, in [the release builder](../docker/release.Dockerfile) |
| Runner | [docker/linux-x86.Dockerfile](../docker/linux-x86.Dockerfile): digest-pinned `i386/debian:bookworm-slim` (glibc 2.36), Python 3.11.2-1+b1 |

Upstream references, checked 2026-10-17:

- [Rust platform support](https://doc.rust-lang.org/rustc/platform-support.html):
  `i686-unknown-linux-gnu` is Tier 1 with host tools, "32-bit Linux (kernel 3.2+,
  glibc 2.17+, Pentium 4)". Its footnote says x87 floating-point return values
  can lose NaN payload bits under the C ABI. The daemon does not depend on NaN
  payloads.
- [Greengrass V2 recipe reference](https://docs.aws.amazon.com/greengrass/v2/developerguide/component-recipe-reference.html):
  the common `Platform.architecture` values are `amd64`, `arm`, `aarch64` and
  `x86`. SuiFleet's native `x86` uses the same name. AWS recipe translation
  still omits and reports `x86` manifests (SF-037). Mapping them is not part of
  this change.

This is clean-room work. No AWS component, binary or source is used or
redistributed. Only the public value names above are used.

## Build and package

```bash
make test-linux-x86                      # cross build + 32-bit runner suites (host, needs Docker)
make release-artifacts RELEASE_TARGET=i686-unknown-linux-gnu RELEASE_OUT=dist-i686
make test-linux-x86 LINUX_X86_BUNDLE=dist-i686 LINUX_X86_EVIDENCE=x86-evidence.json
```

`scripts/release.py build --target i686-unknown-linux-gnu` produces the same
reproducible, SBOM-inventoried bundle as the aarch64 release
([RELEASES.md](RELEASES.md)). Its tarball is
`suifleet-<version>-<commit12>-i686-unknown-linux-gnu.tar.gz` and `--reproduce`
requires two clean builds to be byte-identical. The tag release workflow still
publishes only aarch64. Signing an i686 release in CI needs a workflow change.

On a device, verify a bundle against its own target before installing it:

```bash
suifleet --verify-release BUNDLE /etc/suifleet/release-trust.toml i686-unknown-linux-gnu
```

With the third argument, a correctly signed bundle for any other target is
refused. The target is inside the signed `release.json`, so changing it breaks
the signature.

Yocto image packaging for an x86 machine is not provided. The kas profiles
remain aarch64 (Raspberry Pi, QEMU). Platform scripts own OS installation,
reboot and firmware recovery, as on every platform.

## Component platform selection

- The catalog contract, the daemon and the dApp all accept the manifest
  architecture `x86` (`x86_64|aarch64|arm|x86|*`).
- A 32-bit daemon selects the first `linux|*` × `x86|*` manifest. It never
  selects `x86_64`, and a 64-bit daemon never selects `x86`.
- When a version has no matching manifest, the deployment fails before any
  download.
- The shared vectors in `interfaces/component-cases.json` pin this behaviour
  for Rust and TypeScript.

The contract change is a body-only edit to a private validator, so it is a
compatible upgrade. Older package versions remain callable and still refuse
`x86`. Old daemons validate every manifest of a version, so they also refuse a
version that contains an `x86` manifest ("unsupported manifest platform"). This
fails closed. Publish `x86` manifests only in versions that the whole targeted
fleet runs this daemon or later for, or keep them in separate versions.

Daemon self-update ([DAEMON_UPDATE.md](DAEMON_UPDATE.md)) refuses an ELF
candidate whose class, byte order or machine differs from the running daemon.
It does this before installing the candidate or running its preflight. This
check is needed because an x86_64 kernel also runs i686 binaries, so an
architecture-changing update could otherwise pass its preflight. Candidates
without a valid ELF identification, such as interpreter scripts, are still
judged by the preflight.

## Evidence

`scripts/test_linux_x86.py` (`make test-linux-x86`) runs these steps:

1. **Build.** It cross-builds the debug daemon and its test examples in the
   release builder.
2. **Binary checks.** It asserts ELF32/`EM_386` with the i386 loader and
   records the glibc symbol floor.
3. **Suites.** It runs the existing real-daemon simulator suites with that
   32-bit daemon in the i386 runner, with `--network none` and
   `setarch i686`. The runner checks that it is a 32-bit userland: dpkg
   `i386`, 32-bit Python pointers and no x86_64 loader. It covers:
   - **Identity:** provisioning, preservation, the independent address vector
     and corrupt-key refusal.
   - **OTA** (`test_ota.py`): signing, deployment, failures, offline catch-up,
     revocation and resumed transfer.
   - **Catalog platform selection** (`test_catalog_ota.py`): `x86` selected,
     an `x86_64`-only version refused before any download.
   - **Components and dependencies:** `test_component_ota.py` and
     `test_dependency_ota.py`.
   - **Supervised lifecycle and recovery** (`test_supervisor.py`): crash,
     readiness failure, rollback, daemon SIGTERM/SIGKILL.
   - **Configuration** (`test_configuration.py`).
   - **Daemon self-update** (`test_daemon_update.py`): handoff, rollback,
     preflight rejection, other-architecture refusal, interrupted handoff.
   - **Component IPC** (`test_ipc.py`): root only, to create distinct
     `SO_PEERCRED` UIDs.
4. **Bundle** (with `LINUX_X86_BUNDLE`). It checks that the packaged release
   binary is 32-bit and provisions an identity. It signs a copy of the bundle
   with a throwaway key and verifies it with both binaries. A wrong required
   target and a retargeted manifest are refused.

`make test-x86-catalog-localnet` (`scripts/test_x86_catalog_localnet.py`)
runs on a disposable network-none localnet. It publishes the base contracts,
which refuse `x86`. It then upgrades them in place to the current contracts
with the same UpgradeCap. Afterwards it registers an `x86` manifest on the
existing catalog: the stored ComponentVersion is immutable and keeps its base
type origin. Finally it shows that the old package still refuses `x86`.

The recorded run is [validation/sf076-linux-x86.json](validation/sf076-linux-x86.json).
CI runs `make test-linux-x86` on every change.

## Limitations

- The runner is a 32-bit userland on a 64-bit kernel's compatibility layer.
  These have not been run:
  - a 32-bit kernel, under QEMU system emulation or on hardware;
  - a physical i686 machine;
  - a non-Debian distribution;
  - a production image.
- The shipped systemd units were not exercised on 32-bit. The systemd/cgroup-v2
  harnesses (`test_supervisor_systemd.py`, `test_daemon_update_systemd.py`,
  `test_key_isolation_systemd.py`) remain x86_64-host evidence.
- Only the local chain and Walrus simulators and an isolated localnet were
  used. No public Sui network, real wallet or Walrus service was involved with
  the 32-bit daemon.
- The PKCS#11 feature was built and tested on x86_64 only. It is not part of
  the 32-bit run.
- Native ML runtimes ([ML_COMPONENTS.md](ML_COMPONENTS.md)), AWS-named managed
  components and AWS recipe translation do not target `x86`.
- No AWS account, paid service or credential was used.
