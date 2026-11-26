# Prepare a Raspberry Pi development image

Build a development SD image for Raspberry Pi 3 Model B+ and inspect it before any physical use. No refreshed, physically accepted image download is promised.

## Prerequisites

Use the pinned Yocto build environment, a reviewed checkout, and your own SSH public key. Keep the access overlay private; it must contain no private SSH key, device identity or enrollment secret. Physical flashing is a separate operator-controlled operation.

## Steps

1. Prepare the private SSH access overlay described in the technical image reference below. Replace its example public key with your authorized key.
2. From the checkout, build:

```sh
export KAS_WORK_DIR=/absolute/path/to/yocto-work
export KAS_BUILD_DIR="$KAS_WORK_DIR/build-pi-image"
kas build kas/pi-image.yml:/absolute/path/to/private-access.yml
```

3. Retain the actual image, checksum, package manifest and exact source/layer revisions. Inspect boot/root partitions, wired DHCP, SSH access and included service files before distributing it.
4. After separately authorized flashing and first boot, verify the host fingerprint through a trusted console, read `/etc/suifleet/device.address`, and complete [device setup](INSTALL_DEVICE_CLIENT.md).

## Expected result

A completed build produces a bootable-image artifact with the client included. Build completion alone does not establish physical boot or OTA acceptance.

## Recovery and limits

If no artifact exists, report a failed/incomplete build rather than distributing a filename. Do not erase a used card's keys or journals to repair network configuration. Physical boot, networking, power-loss and firmware recovery remain separately unrun; the unfinished refreshed target image remains on hold.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](PI_IMAGE.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.

## Reusable Testnet starter image

The starter profile targets `raspberrypi3-64` and uses wired DHCP. A signed
starter download is pending. For development builds with preconfigured SSH,
use the separate profile above.

The starter image has no authorized SSH keys. To opt in, use trusted local
access to install your SSH **public** key in
`/home/operator/.ssh/authorized_keys` (directory mode 0700, file mode 0600,
both owned by `operator`). The operator account has administrative access.
For batch onboarding, follow [automatic Linux first boot](ENROLLMENT.md#automatic-linux-first-boot)
to stage the trusted batch configuration and per-unit media after writing
each card.

### Build and verify

On a Linux Yocto build host with kas 4.7, mtools and e2fsprogs, check out the
reviewed source commit and obtain the Testnet deployment manifest from the
console. Use separate new paths on a filesystem with space for two independent
Yocto builds:

```sh
python3 scripts/build_pi_starter.py --manifest /path/to/deployment-manifest.json \
  --work /build/starter-work --out /build/starter-bundle
```

The build uses the public Scarthgap layer and pinned BSP and toolchain. It
checks that the recipe and installed client versions are supported by the
Testnet deployment manifest, checks for the automatic first-boot enrollment
service, and compares the two images and their package inventories. This build
requires an updated public Scarthgap layer revision that includes the
enrollment service. The currently pinned revision predates that service, so
the build stops before producing a bundle. A successful local build produces
an unsigned candidate for validation.

The release pipeline compares both builds before signing and verifies the
signed files before publication. See the [release signing and verification
reference](RELEASES.md) for details.

When a signed bundle is available, collect all its assets in one directory
and verify with a trusted SuiFleet CLI and independently obtained trust roots
before decompressing or flashing:

```sh
suifleet --verify-release /path/to/bundle /path/to/trust.toml raspberrypi3-64-starter
(cd /path/to/bundle && sha256sum -c SHA256SUMS)
```

The signed manifest covers the image, checksums, SPDX package inventory,
provenance and target deployment manifest. Production signing setup and
physical boot and network validation are pending.
