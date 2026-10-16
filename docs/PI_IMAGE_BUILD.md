# SF-029 completed SD-image build evidence

The `raspberrypi3-64` SD image completed through BitBake on 2026-10-16 UTC
(2026-10-15 Pacific). This is a development image for the Pi 3 Model B+.
Physical flashing, Pi boot, actual network/clock/identity persistence and OTA
acceptance remain [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
No physical programming or live-chain transaction was performed by this build.
SF-042 remains on hold. Build success does not establish firmware safety.

## Delivered local artifacts

Linux directory: `/home/tyler/dev/codex/yocto-sf029/artifacts`.
Windows directory: `\\wsl.localhost\Ubuntu-22.04\home\tyler\dev\codex\yocto-sf029\artifacts`.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `suifleet-pi-image-raspberrypi3-64.wic.bz2` | 74511583 | `3261fe2b39ef4354e667d9eee3ddfe864ee8323941d68e5465761f61a6f64b90` |
| Decompressed partitioned WIC | 1085904896 | `2244680d63d5e8b7520299dd502255a0a1489332bf4be4e6b4fcf4c6cccc37c6` |
| Packaged/image daemon | 4737720 | `ee35637ccafa51a0e766a12770e0cf382c444f91208b39d9f83481545f0d666d` |
| Packaged/image harmless updater | 3713 | `ed6f56c9c63ede1e2c71b1db1b40fa1504e13866ff62485cb268187c37dd4400` |

The directory contains `SHA256SUMS`, the WIC block map, `package.manifest`
(1915 packages), three SuiFleet RPMs, license/SPDX data,
`layers.json`, `provenance.json`, inspection/binary-test reports and build logs.
`sha256sum -c SHA256SUMS` passed for every delivered file. Personalized public
keys live only in the private local overlay/image/package, never in Git.
Follow [flashing and first boot](PI_IMAGE.md); select the SD card explicitly.
The image requires at least a 2 GB card and does not automatically grow its root.

## Exact inputs

Metadata verification revision: `4640544df7302f38015c9e5b54a69072ed1c798a`. The later evidence-only
commit changes no recipes. Daemon source: `795de747e066ee97af0e9e0e53deda3c6c71e90c` (reviewed main),
crate version `0.2.1`, recipe package version `1.0`. Cargo.lock SHA-256:
`44aa5b2637fc2b828a796a63e9a550d9c0fa652437cf59ffd31e0b264021290e`; all 302 registry URI/checksums were regenerated with
BitBake and independently compared to that lockfile. Compilation is frozen/offline.

| Layer | Revision |
|---|---|
| poky | `a53cae3de9f45417b97efc4c46c42e4c8ebdb939` |
| meta-openembedded | `ae7dfb12245c7f9b9a353499e2688015bd4e6413` |
| meta-raspberrypi | `2c646d29912dcc873469a57b1c207e1549c5094d` |
| meta-lts-mixins | `eedcdc7486ff15c3510c2ceff68c87d9db141312` |

Kernel source: `e442e5c1ab6bff5b5460b4fc949beb72aaf77970`;
kernel metadata: `52ff0d75713ce61962b325a2090bd55e216f0cf3`.
Boot firmware: `bc7f439c234e19371115e07b57c366df59cc1bc7`;
boot configuration source: `6ac2d832c6c3b208e2669f50ec1abf2c20cb7ff4`.
All four external layer checkouts had no tracked modifications.

Actual tools: BitBake 2.8.1, Rust 1.98.1, Cargo 1.98.0, GCC 13.4.0,
systemd 255.21, Linux 6.6.63-v8, OpenSSH 9.6p1, CA certificates 20260223,
sudo 1.9.17p2, e2fsprogs 1.47.0, mtools 4.0.49 and QEMU user-mode 8.2.7.
Host: Ubuntu 22.04.5 on WSL2. These versions are build evidence, not a
production-release or current-public-network compatibility claim.

## Commands and results

The build uses the equivalent pinned kas settings in an isolated owned build
configuration, with the existing layer checkouts and shared locked downloads/
sstate. `INIT_MANAGER = "systemd"`, `TMPDIR = "${TOPDIR}/tmp-systemd"`,
two BitBake tasks and `PARALLEL_MAKE = "-j4"` were used. The private access layer
installs the user's two public keys only. Exact final build commands:

```sh
source /home/tyler/dev/suifleet-yocto/poky/oe-init-build-env /home/tyler/dev/codex/yocto-sf029/build
bitbake suifleet-pi-image
bitbake -f -c compile virtual/kernel
bitbake suifleet-pi-image
bitbake -f -c compile_kernelmodules virtual/kernel
bitbake suifleet-pi-image
```

The full image run passed all 4836 tasks, 4040 reused. Both normal make rechecks
passed; their final logs contain zero future-timestamp/clock-skew warnings. The
post-recheck image run passed all 4836 tasks, 4821 reused. Verification after the
commits passed all 4836 tasks with all reused, confirming unchanged inputs.
Three expected warnings remain: local hash-equivalence/sstate mirror setup and
two explicit forced-task markers. No package QA or clock check was disabled.

The first attempts exposed and repaired real integration faults: the standard
`operator` group already existed; sudo requires its shared directory mode 0750;
Poky's default SysV init selection had to be explicitly replaced with systemd;
and an incompatible cached libc linker layout referenced `/lib64`. A source
rebuild (`bitbake -c clean glibc`, `bitbake --no-setscene glibc`) passed all 804
tasks including QA. The final profile uses a separate TMPDIR and a pre-staging
AArch64 usrmerge layout guard, so the unvalidated cached variant is excluded.
Cleaning only owned libgcc work restaged the corrected inputs. Shared download/
sstate caches and earlier build trees/logs were retained; only owned recipe work
was cleaned. A SourceForge timeout
recovered through BitBake's checksum-verified mirror fallback. A Meson source
clock check caught a 6.9 ms future timestamp and passed on normal retry.

Offline checks (exit 0):

```sh
python3 -m unittest scripts.tests.test_inspect_pi_image scripts.tests.test_test_component
python3 -m py_compile scripts/inspect_pi_image.py scripts/test_pi_binary.py
git diff --check
```

All 19 tests passed. Both kas profiles and the private overlay passed kas 4.7
JSON-schema validation. `provenance.json` records the complete expanded image
inspection and binary-test commands, tool paths, package hashes and source pins.

The actual WIC passed MBR/FAT/ext4 inspection and read-only `e2fsck -f -n`.
Boot files include 64-bit configuration, `kernel8.img`, the Pi 3 B+ DTB,
`start.elf`, `fixup.dat` and an SD-root command line. The daemon matches its packaged binary and the updater matches its source
script; the daemon is AArch64 ELF64. The inspection verified:

- No device key/address, actual enrollment/config/manifest/policy, daemon state,
  baked SSH host key, static machine identity or legacy ifupdown configuration.
- Wired networkd DHCP, resolved DNS and timesyncd enabled with real unit targets.
- Root/password SSH disabled; operator public-key file exactly matches both
  authorized public keys, with correct account/home/key permissions and sudo.
- Provisioning/enrollment services installed; the unprivileged daemon is gated
  on actual enrollment; CA certificates are installed.

The actual rootfs daemon ran in QEMU user mode with disposable private temporary
files: fresh identities differed, an existing identity survived repeated
provisioning, address derivation matched, and corrupt identity was refused
without replacement. Generated keys were discarded. This does not boot the Pi,
exercise systemd in the guest or establish real-device reboot persistence.

## First boot handoff

Hostname `suifleet-pi`, user `operator`, SSH public-key authentication only.
The authenticated operator has explicit passwordless sudo for this development
image. Use the router's DHCP lease list for the actual wired address; no static
IP or Wi-Fi credentials are configured. Verify `timedatectl status` reports
clock synchronization before testnet TLS/enrollment; actual Pi synchronization
has not been observed. Obtain the public signer address after provisioning,
install a verified manifest/config bundle last, and opt into the harmless
`/usr/bin/suifleet-test-updater` in direct mode for the one-shot application test.
Preserve `/etc/suifleet/device.key` and `/var/lib/suifleet`. Physical boot, SSH,
network/time, restart identity/journal persistence and live OTA remain SF-031.
