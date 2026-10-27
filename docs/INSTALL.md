# Install the SuiFleet device client

SuiFleet is a public-testnet preview. Choose a platform below, then enroll the
device and send a small test component before a wider rollout. You need an
operator account, funds for network fees, a deployment manifest for that same
network and administrative access to the device. Registration alone does not
install or start the client.

## Choose an installation

| Platform | Installation path |
|---|---|
| Raspberry Pi / Yocto | Use the [Pi image guide](PI_IMAGE.md) for image creation and inspection, or add the pinned [Yocto recipe](YOCTO_PACKAGING.md) to your own image. Physical flashing and boot acceptance remain separate; no universally validated downloadable image is promised here. |
| macOS | Follow the [macOS installer and LaunchDaemon guide](MACOS_RUNTIME.md). The repository installer provisions a local identity but writes no fleet configuration and starts no service. |
| Generic Linux | Build the client from the repository as below, then package it with the [Yocto service and signer boundaries](YOCTO_PACKAGING.md), or integrate it into your platform service manager. This is a source installation, not a distribution package repository. |

## Generic Linux source installation

Use a supported Linux host with Git, Docker Engine/Compose and GNU Make.
Get the public source (no GitHub account is needed), select a reviewed release
revision, and build with the repository's pinned toolchain:

~~~bash
git clone https://github.com/tyler-suifleet/suifleet.git
cd suifleet
# Select your reviewed revision before building.
make dev
make edge-build
~~~

The build produces the daemon under suifleet-edge/target/release/suifleet.
This source build is for evaluation. For distributable bundles use the
[release build and signature-verification procedure](RELEASES.md); provision
trust roots through your own authenticated channel. Do not download a binary
from an arbitrary link or skip release verification.

Before starting a Linux service, integrate the daemon and the separate signer
using the [packaging instructions](YOCTO_PACKAGING.md) and
[key-isolation guide](KEY_ISOLATION.md). Do not run component commands as root.
Create device keys only on the target at runtime, never in a reusable image.
The software-key identity is not an administrator wallet recovery phrase.

## Configure and enroll

1. Obtain the operator's [deployment manifest](DEPLOYMENT_MANIFEST.md). Verify
   network, package and shared-object IDs; a manifest from another network is
   rejected.
2. Prepare runtime configuration from the repository's
   [configuration example](../suifleet-edge/config.example.toml). Install it as
   /etc/suifleet/config.toml with the manifest at its configured path.
   Keep private credentials out of public images and configuration downloads.
3. Provision the device identity using your platform's first-boot service.
   Register its **public address** in the console, or follow
   [claim enrollment and provisioning media](ENROLLMENT.md). A claim credential
   is single-use bootstrap material, not an operator key. Never publish it.
4. Configure the [fleet budget](FLEET_BUDGET.md) or other documented device
   report-gas funding. Protect /etc/suifleet/device.key and preserve
   /var/lib/suifleet across service and platform restarts.
5. Start the platform service only after configuration and enrollment. On the
   packaged systemd path, inspect systemctl status suifleet.service and
   journalctl -u suifleet.service; on macOS use the LaunchDaemon commands in
   its guide. Never paste keys, PINs or claim files into support logs.

## Send a first component

Open the console's Components page, publish a version for the device's OS and
architecture, then target the registered device or group. Follow
[publishing and deployment](COMPONENT_CATALOG.md). Use harmless application
content first. Confirm an authenticated device report and inspect failures;
a submitted transaction digest alone does not establish execution success.

If the device stays offline, check network access, manifest compatibility,
enrollment and report-gas funding before retrying. Preserve journals; an unknown
installer outcome needs operator inspection, not automatic replay.

[Configured platform installers](PLATFORM_INSTALLERS.md) own OS installation,
reboot decisions and firmware recovery. A successful command does not prove a
healthy boot. Physical Pi, production provider, real-wallet and mainnet acceptance
remain separate from this software guide.
