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
