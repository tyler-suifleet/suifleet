# SF-066 signer-service SD-image build evidence

The `raspberrypi3-64` SD image was rebuilt through BitBake on 2026-10-22 UTC
(2026-10-21 Pacific) from reviewed main `d616fa16e1d3e90eb0f97a669f693dec99546d3c`.
That source contains the SF-053 signing service, the SF-026 launcher/self-update
integration, SF-022 Seal sessions and current signed telemetry. This is a
development image for the Pi 3 Model B+. Physical flashing, Pi boot, actual
network/clock/identity persistence, recovery and OTA acceptance remain
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32). No device was
programmed and no live-chain transaction was performed. Build success does not
establish firmware safety; platform scripts still own reboot and recovery.

The [SF-029 image](PI_IMAGE_BUILD.md) is retained unchanged as an older public
smoke baseline; its hashes do not describe this release.

## Delivered local artifacts

Linux directory: `/home/tyler/dev/codex/yocto-sf066/artifacts`.
Windows directory: `\\wsl.localhost\Ubuntu-22.04\home\tyler\dev\codex\yocto-sf066\artifacts`.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `suifleet-pi-image-raspberrypi3-64.wic.bz2` | 75888483 | `d7f795b68d99e8598b5216208c71d48debc70f1cbf056f204c597ddfe58430f8` |
| Decompressed partitioned WIC | 1091357696 | `7fdb1d7dfedda1696fdc1957551c25ab17173304f45a655b1ca4b5e895ec972b` |
| Packaged/image daemon `/usr/bin/suifleet` | 7427280 | `73a739091394f17f9ee8c9e667a0cecb53c0b71461992b66a2b217f1c5f51b58` |
| Packaged/image harmless updater | 3713 | `ed6f56c9c63ede1e2c71b1db1b40fa1504e13866ff62485cb268187c37dd4400` |
| `suifleet-1.0-r0.cortexa53.rpm` | 4197499 | `16fefbc989e3233d3aaf0d06fe9bed779451f1b3c5f37ec989e4aaa4ba6e69d1` |
| `SHA256SUMS` | | `a74bf26dac6511de72c5bfbe9b35c02d702956b178503e11591c9609abcfff89` |

The directory also contains the WIC block map, `package.manifest` (1915
packages), the three SuiFleet RPMs, license/SPDX data, `layers.json`,
`provenance.json`, `inspection.json`, `binary-test.json` and both BitBake logs.
`sha256sum -c SHA256SUMS` passed for all 15 delivered files. Personalized
public keys exist only in the private local overlay/image/package, never in Git.
Follow [flashing and first boot](PI_IMAGE.md); select the SD card explicitly.

## Exact inputs

Layer metadata: commit `410e123a35b9b653f95d13509742738a098e13b0`; later commits
in this change touch only scripts and documentation. Daemon source:
`d616fa16e1d3e90eb0f97a669f693dec99546d3c`, crate version `0.2.1`, recipe
package version `1.0`. Cargo.lock SHA-256:
`310102d73d7644815fe05df24917c332136836d64dcff5f7ca35b44c63b2d937`. The actual
unpacked source HEAD and lock digest were checked in the BitBake work tree.
`bitbake -c update_crates suifleet` regenerated `suifleet-crates.inc`; all 349
registry crate URIs and SHA-256 values were independently compared to that
lockfile and match exactly. Compilation is frozen/offline.

Layer revisions equal `kas/packaging.yml`, with no tracked modifications:
poky `a53cae3d…`, meta-openembedded `ae7dfb12…`, meta-raspberrypi `2c646d29…`,
meta-lts-mixins `eedcdc74…` (full revisions in `layers.json`). Verified compiler:
rustc 1.98.1. Selected packages: Linux 6.6.63-v8, systemd 255.21, OpenSSH 9.6p1,
sudo 1.9.17p2, CA certificates 20260223. These are build evidence, not a
production-release or current-public-network compatibility claim.

## Commands and results

The owned build directory `/home/tyler/dev/codex/yocto-sf066/build` uses the
SF-029 configuration (the pinned kas settings, `INIT_MANAGER = "systemd"`,
`TMPDIR = "${TOPDIR}/tmp-systemd"`, two BitBake tasks, `-j4`) with this
checkout's layer and the private public-key-only access overlay:

```sh
umask 022
source /home/tyler/dev/suifleet-yocto/poky/oe-init-build-env /home/tyler/dev/codex/yocto-sf066/build
bitbake -c update_crates suifleet   # 336 tasks, 332 reused, all succeeded
bitbake suifleet-pi-image           # 4836 tasks, 2842 reused, all succeeded
```

The only warning is the local hash-equivalence/sstate mirror setup. No QA,
disk or clock check was disabled. The earlier a80784b attempt, which halted on
host disk space, is retained under `attempt-a80784b/`; it produced no image.

Verification (all exit 0; commands with full paths are in `provenance.json`):

```sh
python3 -m unittest scripts.tests.test_inspect_pi_image scripts.tests.test_test_component
python3 -m py_compile scripts/inspect_pi_image.py scripts/test_pi_binary.py
python3 scripts/inspect_pi_image.py ARTIFACTS/suifleet-pi-image-raspberrypi3-64.wic.bz2 \
  --mtools-dir … --debugfs … --e2fsck … --expected-authorized-keys … \
  --recipe suifleet-edge/meta-suifleet/recipes-edge/suifleet/suifleet_1.0.bb --output ARTIFACTS/inspection.json
python3 scripts/test_pi_binary.py --qemu …/qemu-aarch64 --rootfs …/suifleet-pi-image/1.0/rootfs --output ARTIFACTS/binary-test.json
sha256sum -c SHA256SUMS
```

The actual WIC passed MBR/FAT/ext4 inspection and read-only `e2fsck -f -n`.
Boot files include 64-bit configuration, `kernel8.img`, the Pi 3 B+ DTB,
`start.elf`, `fixup.dat` and an SD-root command line. The inspection verified:

- `build-info` names exactly the recipe's source revision and lock digest.
- No device key/address, enrollment/config/manifest, `signer.toml`, PKCS#11 PIN,
  environment file, signer runtime directory, daemon state, baked SSH host key,
  machine identity or legacy ifupdown configuration.
- Separate `suifleet` and `suifleet-signer` accounts; the daemon user is not in
  the signer group. `/etc/suifleet` is root:suifleet 0750; the config example
  root:suifleet 0640 and selects `kind = "service"`; the signer example
  root:suifleet-signer 0640; the daemon binary and provisioning helper are
  root-owned 0755.
- `suifleet.service` starts `/usr/bin/suifleet --launch`, records its main
  process as root with the base binary, uses `NotifyAccess=main` and
  `KillMode=mixed`. The signer unit runs as `suifleet-signer` with
  `--signer-service`, `IPAddressDeny=any` and `NoNewPrivileges=yes`, gated on an
  installed `signer.toml`.
- Enabled with real unit targets: networkd (wired DHCP, no static address),
  resolved, timesyncd, provisioning, enrollment path, daemon, firmware path,
  signer service and signer socket; SSH enabled.
- Root/password SSH disabled; operator public-key file exactly matches the
  overlay, with correct ownership/modes and development sudo.

Every installed SuiFleet unit, the provisioning script and the three
configuration examples are byte-identical to the layer files; the rootfs daemon
is byte-identical to the packaged one. A text scan of `/etc`, `/home`, `/root`
and `/var` found no private-key material.

The actual rootfs daemon (AArch64 PIE) ran in QEMU user mode against disposable
private temporary files: fresh identities differed, an existing identity
survived repeated provisioning, address derivation matched and corrupt identity
was refused. `--self-check` accepted the mixed-origin fixture manifest and
refused unsupported protocol and edge-client ranges. `--key-holder` selected
`suifleet` for a legacy key and `suifleet-signer` for the service signer, and
`[daemon_update] enabled = true` with the service signer was refused. Keys were
discarded. This does not boot the Pi or exercise systemd in the guest.

## Compatibility boundary

Launcher and signer are modes of `/usr/bin/suifleet`, not separate binaries.
The reviewed security boundary rejects enabled daemon self-update with the
service signer ([DAEMON_UPDATE.md](DAEMON_UPDATE.md)); this image preserves
that restriction, so self-update is usable only with a legacy daemon-held key.
No reusable session credential is installed or passed to the daemon; Seal
session keys exist only in signer memory. The image ships no enrollment,
release-trust policy or production credentials.

## Deferred

Physical flashing, Pi boot, SSH/DHCP/time on real hardware, signer and daemon
start under the guest's systemd, identity/journal persistence across reboot,
power loss and recovery remain [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
