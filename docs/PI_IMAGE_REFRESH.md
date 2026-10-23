# Raspberry Pi SD-image build evidence

## SF-120 archive-runtime image (current)

The `raspberrypi3-64` SD image was rebuilt through BitBake on 2026-10-23 UTC
from merged main `2d231cadf3c0c8e58b48dc9f48dd7890faaafa1f`, which contains
SF-082 (merge `39ea150a`) and SF-084 (merge `2d231cad`). It adds the encrypted
archive runtime: the official Node.js 22.22.2 arm64 binary and the SF-082
Walrus codec built from the same revision
([archive runtime](YOCTO_PACKAGING.md#serverless-archive-runtime-sf-120)).
This is the image for final testing. It was not flashed or booted on hardware,
and no live-chain transaction was made. Physical acceptance remains
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and mainnet
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).

It supersedes the SF-066 image below (daemon `d616fa16`, no archive runtime)
and the provisional SF-120 image in `/home/tyler/dev/codex/yocto-sf120/provisional`
(daemon `d616fa16`, WIC `394fcaac…955d`), which must not be flashed.

### Delivered local artifacts

Linux directory: `/home/tyler/dev/codex/yocto-sf120/artifacts`.
Windows directory: `\\wsl.localhost\Ubuntu-22.04\home\tyler\dev\codex\yocto-sf120\artifacts`.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `suifleet-pi-image-raspberrypi3-64.wic.bz2` | 115752625 | `9d267e869d3ee9856ca939c0938bb24304504601104790d5d812124a2771f037` |
| Decompressed partitioned WIC | 1304008704 | `9c4c8a462f73671b73bc64db2a9001bf51844b7a03b89008ae9769f08d49e458` |
| Image daemon `/usr/bin/suifleet` | 7689664 | `cad45ac83282b8616cde4a5584b5c40221a8249b91f2219606d29208b0e1deb2` |
| Image Node `/usr/lib/suifleet/node/bin/node` | 121980552 | `3159f9115ab4be7d318b7c28e946837a4dceb7f2b3c43232aa2f2e3852550b90` |
| Codec `walrus-device-codec.cjs` | 896673 | `44cdaf7e616523f593ea05f1a64447ef37d1893643108221c8913481f1d83ec4` |
| Codec `walrus_wasm_bg.wasm` | 358530 | `649f85e5306ebb14e5d36b1edfdb03be5deef4b5e21c9ed06ca030b03b41335a` |
| `suifleet-1.0-r0.cortexa53.rpm` | 4357484 | `b6aeefb2fca98adf153d718fddf81f57ebba5b2d25ddb00f759b9675e0bdd668` |
| `suifleet-node-22.22.2-r0.cortexa53.rpm` | 41660453 | `66c1e1ff23863734199f4b665ae6874055b0e514fedba90cedd28b868a71c1e8` |
| `suifleet-walrus-codec-1.0-r0.cortexa53.rpm` | 366058 | `3c57cdefc919c6a077005d2c1622d0941a09147f536e6b18b9c944de289a6f4f` |
| `SHA256SUMS` | | `3d2a088f0516cfda4f8791f48de8edd1a65f00214324f75189f2b1fed28c67ee` |

The directory also has the block map, `package.manifest` (1918 packages),
the five SuiFleet RPMs, SPDX license data, `layers.json`, `provenance.json`,
`inspection.json`, `binary-test.json`, `codec-test.json` and the BitBake logs.
`sha256sum -c SHA256SUMS` passed for all 21 files. The private access overlay
(two authorized public keys, outside Git) is the same as for SF-066. Flash
following [PI_IMAGE.md](PI_IMAGE.md); select the SD card explicitly.

### Exact inputs and commands

Layer metadata: commit `41fc9da` of this change; later commits touch only
documentation. `bblayers.conf` of `/home/tyler/dev/codex/yocto-sf120/build`
names this reviewed checkout's `meta-suifleet`, not an older worktree. Poky,
meta-openembedded, meta-raspberrypi and meta-lts-mixins equal the
`kas/packaging.yml` pins with no tracked changes (`layers.json`); rustc 1.98.1.
The unpacked daemon source HEAD is `2d231cad`, crate 0.2.1, and its Cargo.lock
SHA-256 is `310102d7…2d937` (unchanged from SF-066). `update_crates`
regenerated an identical include; all 349 registry crates and checksums were
compared to the lockfile independently.

```sh
umask 022
source /home/tyler/dev/suifleet-yocto/poky/oe-init-build-env /home/tyler/dev/codex/yocto-sf120/build
bitbake -c update_crates suifleet            # 336 tasks, all succeeded
bitbake -c cleansstate suifleet-walrus-codec # clears the taint of an earlier deliberate tamper test
bitbake suifleet-pi-image                    # 4871 tasks, 4845 reused, all succeeded
```

The only warning is the local hash-equivalence/sstate mirror setup.
`do_verify_codec`, `do_verify_lock` and `do_verify_toolchain` ran and passed.

### Verification

All exit 0; full paths are in `provenance.json`:

```sh
python3 scripts/inspect_pi_image.py ARTIFACTS/suifleet-pi-image-raspberrypi3-64.wic.bz2 \
  --mtools-dir … --debugfs … --e2fsck … --expected-authorized-keys … \
  --recipe suifleet-edge/meta-suifleet/recipes-edge/suifleet/suifleet_1.0.bb --output ARTIFACTS/inspection.json
python3 scripts/test_pi_binary.py --qemu …/qemu-aarch64 --rootfs …/rootfs --output ARTIFACTS/binary-test.json
python3 scripts/test_pi_codec.py --qemu …/qemu-aarch64 --rootfs …/rootfs \
  --rpc-url https://fullnode.testnet.sui.io:443 --host-node …/node-v22.22.2-linux-x64/bin/node \
  --output ARTIFACTS/codec-test.json
sha256sum -c SHA256SUMS
```

- The inspection covers everything listed for SF-066 below. `build-info` names
  `2d231cad` and the lock digest; the codec's provenance names the same
  revision; the codec files match the recipe pins and Node is the packaged one.
  No device key/address, enrollment config, manifest, signer config, PIN,
  daemon state or baked host key; read-only `e2fsck` passed.
- The rootfs daemon and Node equal their packaged files, and Node equals the
  official `node-v22.22.2-linux-arm64` binary. The rootfs has glibc,
  `libstdc++` and `libgcc_s`. A text scan of `/etc`, `/home`, `/root`, `/var`
  and `/usr/lib/suifleet` found no private-key material.
- The daemon identity checks passed in user-mode QEMU as for SF-066.
- The target Node/codec reported v22.22.2 and refused malformed and
  foreign-chain requests and a missing or corrupt WASM. A read-only Testnet
  `prepare` of a deterministic 1 MiB ciphertext gave the same blob ID and root
  hash on the target and on official x86-64 Node 22.22.2, so the target encodes
  exactly as the host. The blob ID commits to every sliver, which is what
  Walrus readers use to recover the ciphertext.

Not covered: Pi boot, systemd in the guest, writes to storage nodes, the
daemon's own codec-checksum refusal on target, Seal encryption on target and
codec memory on the Pi (see the 256M `MemoryMax` note in the archive runtime
section).

### Keeping existing device state through a later reflash

The image has no package manager, so moving an already-enrolled SF-066 card to
this image means reflashing, which erases its root filesystem. Only do this
under an authorized SF-031 session. To keep the device identity and journals:

1. Stop the enrollment path first, then the daemon and signer:
   `sudo systemctl stop suifleet-enrollment.path suifleet.service suifleet-signer.socket suifleet-signer.service`.
2. As root, archive `/etc/suifleet` and `/var/lib/suifleet` with ownership by
   name and modes: `sudo tar -cpf /tmp/suifleet-state.tar /etc/suifleet /var/lib/suifleet`.
   Copy it to private storage outside Git, logs and issues; it contains the
   device key. Record `sudo cat /etc/suifleet/device.address`.
3. Flash and boot the new image without network if possible. Stop the
   enrollment path and services again, then restore as root by name (not
   numeric IDs, which can differ between images):
   `sudo tar -xpf suifleet-state.tar -C /`. This replaces the fresh identity
   that first boot generated; that key was never registered.
4. Check that `device.address` matches the recorded address and that
   ownership/modes match [KEY_ISOLATION.md](KEY_ISOLATION.md) before starting
   `suifleet-provision.service` and the enrollment path. Provisioning keeps
   an existing valid key and refuses a corrupt one.
5. Delete the archive from temporary locations afterwards.

Encrypted archives stay off until `[fleet_data.archives]` is configured with
the packaged paths and the codec SHA-256 from its `manifest.json`. Opening
SF-066 daemon state with the newer daemon was not tested on target here; check
it in SF-031 before relying on it.

## SF-066 signer-service image (superseded)


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

### Delivered local artifacts

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

### Exact inputs

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

### Commands and results

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

### Compatibility boundary

Launcher and signer are modes of `/usr/bin/suifleet`, not separate binaries.
The reviewed security boundary rejects enabled daemon self-update with the
service signer ([DAEMON_UPDATE.md](DAEMON_UPDATE.md)); this image preserves
that restriction, so self-update is usable only with a legacy daemon-held key.
No reusable session credential is installed or passed to the daemon; Seal
session keys exist only in signer memory. The image ships no enrollment,
release-trust policy or production credentials.

### Deferred

Physical flashing, Pi boot, SSH/DHCP/time on real hardware, signer and daemon
start under the guest's systemd, identity/journal persistence across reboot,
power loss and recovery remain [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
