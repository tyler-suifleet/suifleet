# Install the device client

The SuiFleet device client is the `suifleet` daemon. It runs on each device,
uses the device's own identity, watches the network for deployments aimed at it and
reports results. The console links here from Get started, Devices and
Enrollment. This guide is also published in the public installation hub;
reading it does not require repository access.

A device needs five things:

1. The `suifleet` binary and its service files.
2. A device key, generated on the device. The client prints its public address.
3. The network manifest `deployment.json` for the fleet's network.
4. On Linux, the signing service's configuration, `/etc/suifleet/signer.toml`.
   The signing service holds the device key; the daemon cannot start without it.
5. The daemon's configuration, `/etc/suifleet/config.toml`.

Items 4 and 5 are written after the device is registered or enrolled, and
installed in that order: `deployment.json`, then `signer.toml`, then
`config.toml` last.

## Prerequisites

You need administrative access to the device, the correct fleet console/network,
an operator account with registration authority, and funds for enrollment and reports.
Keep the Device and DeviceCap IDs distinct from the device public address.

## Get the client

The client is built from this repository's `suifleet-edge` crate. **No
prebuilt download is published yet.** The repository can produce signed,
reproducible release bundles ([release verification](CUSTOMER_RELEASES.md)), but none have been
released for operators to download. Ask your fleet operator for an authenticated
release bundle or use the source-build paths below. Verify signed bundles using [release verification](CUSTOMER_RELEASES.md)
and trust roots supplied through an authenticated channel before installation.
Do not install unverified binaries.

| Platform | How to get it | Status |
|---|---|---|
| Raspberry Pi and other Yocto targets | [Yocto image and recipe](#yocto-image-and-recipe) | Image builds and QEMU boot recorded; physical Pi acceptance pending (physical-target validation is still pending) |
| macOS 15 on Apple silicon | [macOS package](#macos-package) | Validated on a hosted macOS 15 runner, not a physical Mac |
| Debian 12 / Ubuntu 24.04 (amd64, arm64; Linux ≥6.5) | [Debian and Ubuntu](#debian-and-ubuntu) | Buildable local-file packages; no public APT repository/download yet |
| Other Linux with systemd | [Generic Linux](#generic-linux) | Manual install of the same files as the Yocto recipe; not a tested distribution package |

Windows and 32-bit x86 Linux have their own notes in
[WINDOWS_RUNTIME.md](WINDOWS_RUNTIME.md) and [LINUX_X86.md](LINUX_X86.md).

Never copy a device key between devices or into an image, package or
repository. Each device generates its own at first boot or install.

## Yocto image and recipe

A standalone public `meta-suifleet` repository and OpenEmbedded Layer Index
listing are not published yet. Do not guess a clone URL. Until those destinations
are published, obtain the pinned layer and kas profiles from your fleet operator
or a reviewed public source checkout. The commands below run from that checkout.

The `meta-suifleet` layer packages the client as the `suifleet` recipe, with
first-boot key provisioning and systemd services. Build a Raspberry Pi 3 Model
B+ SD image with kas 4.7 on Ubuntu 22.04:

```sh
export KAS_WORK_DIR=/absolute/path/to/yocto-work
export KAS_BUILD_DIR="$KAS_WORK_DIR/build-pi-image"
kas build kas/pi-image.yml:/absolute/path/to/private-access.yml
```

The private overlay adds your SSH public key; see [Pi image guide](CUSTOMER_PI_IMAGE.md)
for it, flashing and first login. To add only the client to your own image,
add `suifleet-edge/meta-suifleet` to your layers and `suifleet` to the image's
packages; [Yocto guide](CUSTOMER_YOCTO.md) lists the pinned inputs and
package-only builds.

The recipe now pins client 0.2.3, compatible with the committed Seal testnet
profile. Older evaluation images contain client 0.2.1 and cannot use a manifest
requiring 0.2.3; rebuild from the updated recipe. Do not lower the manifest's
client range to use an old image. See [Yocto software build evidence](YOCTO_PACKAGING.md)
for rebuilt package scope; a new bootable SD image and physical Pi acceptance
must be recorded separately. `make test-yocto-packaging` checks profile/version
drift before building or flashing.

At first boot `suifleet-provision.service` generates the key, without
networking, and prints the address (see
[Find the device address](#find-the-device-address)). The image includes
`/etc/suifleet/signer.toml.example` and `/etc/suifleet/config.toml.example` as
templates. The signing service stays stopped until `/etc/suifleet/signer.toml`
exists, and the daemon `suifleet.service` until `/etc/suifleet/config.toml`
exists.

## macOS package

Build and install as root:

```sh
cargo build --locked --release --manifest-path suifleet-edge/Cargo.toml
sudo suifleet-edge/packaging/macos/install.sh suifleet-edge/target/release/suifleet
```

The installer creates the `_suifleet` account, installs
`/usr/local/bin/suifleet` and the LaunchDaemon, generates
`/etc/suifleet/device.key` once and prints the address. It writes no
configuration and starts nothing. After writing `deployment.json` and
`config.toml` (root-owned, 0644), start the service:

```sh
sudo launchctl bootstrap system /Library/LaunchDaemons/org.suifleet.daemon.plist
```

Service control, logs and limits are in [macOS guide](CUSTOMER_MACOS.md).

## Debian and Ubuntu

Use a verified local-file package or the signed APT snapshot from your operator.
See [Debian packaging](DEBIAN_PACKAGING.md) for supported profiles, exact version
pinning and data retention. Packages require systemd and Linux 6.5 or later for
the isolated signer.

## Generic Linux

Select and authenticate a [versioned bundle](CUSTOMER_RELEASES.md). Extract
the manifest-listed inner archive into a staging directory as an unprivileged
user. These commands assume the extracted `suifleet` directory is in your
current directory and that the two dedicated system accounts do not yet exist.
On upgrades reuse those accounts, stop the running units before replacing
files, and preserve existing configuration, identity and journals.

```sh
F=suifleet/systemd
sudo useradd --system --no-create-home --home-dir /var/lib/suifleet --shell /usr/sbin/nologin --user-group suifleet
sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --user-group suifleet-signer
sudo install -d -m 0750 -o root -g suifleet /etc/suifleet
sudo install -d -m 0700 -o suifleet -g suifleet /var/lib/suifleet
sudo install -m 0755 suifleet/bin/suifleet /usr/bin/suifleet
sudo install -D -m 0755 suifleet/libexec/suifleet-provision /usr/libexec/suifleet-provision
sudo install -m 0644 "$F"/suifleet*.service "$F"/suifleet*.path "$F"/suifleet*.socket /etc/systemd/system/
sudo install -m 0640 -o root -g suifleet-signer "$F/signer.toml.example" /etc/suifleet/signer.toml.example
sudo install -m 0640 -o root -g suifleet "$F/config.toml.example" /etc/suifleet/config.toml.example
sudo systemctl daemon-reload
sudo systemctl start suifleet-provision.service
```

Install the authenticated release trust file root-owned (0644). Provision and
enroll below, then enable the signer socket and enrollment path as described
under [signing service configuration](#signing-service-configuration-signertoml).
The daemon binary also implements the signer command. The provisioning helper
and the systemd service, socket and path definitions are its companion files.
Platform updater tools are supplied separately by the platform operator.

The included units require systemd and the isolated signer requires Linux 6.5
or later with the shipped caller identity checks. On a non-systemd or older
kernel, the archive can run offline CLI operations on the supported ABI, but
the isolated signer and enrollment path are outside that profile. The existing
service signer also remains incompatible with daemon self-update, including on
systemd hosts. An external supervisor would need independent lifecycle,
privilege and signer validation before fleet use. Do not substitute a
daemon-readable key for an isolated or user-owned signer requirement.
Follow [key isolation](KEY_ISOLATION.md) for the current signer and PKCS#11
build requirements. The standard bundles use the current service signer
and do not enable the optional PKCS#11 feature. For user-owned hardware keys,
use a build with that feature and validate the operator's token integration.
Track older-kernel signing in [SF-292](https://github.com/tyler-suifleet/suifleet/issues/818),
non-systemd supervision in [SF-293](https://github.com/tyler-suifleet/suifleet/issues/829)
and isolated-signer self-update in [SF-299](https://github.com/tyler-suifleet/suifleet/issues/831).
Standalone layer publication and older Yocto LTS support are tracked in
[SF-295](https://github.com/tyler-suifleet/suifleet/issues/820) and
[SF-309](https://github.com/tyler-suifleet/suifleet/issues/835).

## Find the device address

The device address is the public Sui address of the device's own key. It is
not your wallet address, and it is safe to share.

When the key is first created, the client prints one line:

```text
SuiFleet device address: 0x3f2a…   (0x followed by 64 hexadecimal characters)
```

You see this line from the macOS installer, and on Linux in the provisioning
journal. Read it again at any time:

```sh
sudo cat /etc/suifleet/device.address
journalctl -u suifleet-provision.service     # Yocto image and generic Linux
sudo suifleet --derive-address /etc/suifleet/device.key
```

Copy the whole value, from `0x` to the end, into the console. Nothing else
printed by provisioning is secret, but `device.key` is: never display or copy
it.

## Network manifest (deployment.json)

`deployment.json` names the network, the SuiFleet package and its shared
objects. The device refuses to start against a manifest for a different chain
or a superseded release. Get it from your fleet's operator or publisher:

- The console's **Download connection settings** button saves `deployment.json`,
  the manifest this console was built with. It does not include the device's
  Device or DeviceCap IDs, signer configuration or daemon configuration.
- Enrollment media downloaded from the Enrollment page already include it.
- A publisher's release directory `deployments/ENV/current/` contains it
  ([DEPLOYMENT_MANIFEST.md](DEPLOYMENT_MANIFEST.md)).

Install it first, as `/etc/suifleet/deployment.json`, root-owned. On Linux use
group `suifleet`, mode 0640.

## Signing service configuration (signer.toml)

On Linux the device key belongs to the signing service
(`suifleet-signer.service`), not to the daemon, so components the daemon runs
cannot read it ([signing service](CUSTOMER_KEY_ISOLATION.md)). The daemon asks this
service for the device address when it starts, so install
`/etc/suifleet/signer.toml` **before** `config.toml`. Its appearance also
hands the key file to the service's user. A minimal file:

```toml
# /etc/suifleet/signer.toml
manifest_path = "deployment.json"
device_cap_id = "0x<DEVICE_CAP_ID>"
keypair_path = "/etc/suifleet/device.key"
caller_unit = "suifleet.service"
```

Use the same `manifest_path` and DeviceCap ID as `config.toml`. Write it from
`/etc/suifleet/signer.toml.example`, which also lists the optional Seal, fleet
budget and PKCS#11 settings, and install it root-owned, group
`suifleet-signer`, mode 0640:

```sh
sudo install -m 0640 -o root -g suifleet-signer signer.toml /etc/suifleet/signer.toml
```

macOS has no signing service: the daemon reads the key itself (see
[macOS guide](CUSTOMER_MACOS.md)), so skip this step there.

## Configuration file

`/etc/suifleet/config.toml` names the manifest, the device's on-chain IDs and
how updates are applied. Install it **last**, after `deployment.json` and
`signer.toml`: on Linux, its appearance starts the daemon. A minimal Linux
configuration with the signing service:

```toml
# /etc/suifleet/config.toml
[sui]
manifest_path = "deployment.json"

[device]
device_object_id = "0x<DEVICE_OBJECT_ID>"
device_cap_id = "0x<DEVICE_CAP_ID>"

[device.signer]
kind = "service"
socket = "/run/suifleet-signer/sign.sock"

[apply]
command = "/usr/bin/false"   # choose your installed updater explicitly
timeout_seconds = 600
allow_recipe_commands = false

[runtime]
state_dir = "/var/lib/suifleet"
```

Install it root-owned, group `suifleet`, mode 0640:

```sh
sudo install -m 0640 -o root -g suifleet config.toml /etc/suifleet/config.toml
```

On macOS, replace the `[device.signer]` table with
`keypair_path = "/etc/suifleet/device.key"` under `[device]`, and install both
files root-owned, mode 0644.

The Device and DeviceCap IDs come from registration or enrollment. Every
option is documented in
[`suifleet-edge/config.example.toml`](../suifleet-edge/config.example.toml).
`suifleet --check-config PATH` loads a configuration, verifies its manifest
against the network and prints `Verified deployment configuration`:

```sh
sudo suifleet --check-config /etc/suifleet/config.toml
```

## First-boot enrollment

With Enrollment, the device registers itself; you copy no address. On the
Enrollment page, create a policy, reserve names and download the credential
media. Copy each device's `enroll.toml`, `claim.key` and `deployment.json` to
its provisioning media, then on the device after provisioning run:

```sh
sudo suifleet --enroll /path/to/enroll.toml
```

It prints JSON with `device_address`, `device_object_id` and `device_cap_id`.
Write the DeviceCap ID into `signer.toml` and both IDs into `config.toml`, then
install them in that order. Details, claim funding and
security are in [Enrollment](CUSTOMER_ENROLLMENT.md).

## Register by address

Without Enrollment, register one device at a time:

1. Read the device address as above.
2. In the console, use Get started, or Devices → Register by address. Paste the
   address and choose the architecture. Your wallet approves the transaction.
3. Copy the Device object ID from the Devices page (show technical details).
   Registration sends the DeviceCap to the device address, so read its ID from
   the registration transaction's created objects in a Sui explorer, or list
   the objects that address owns (`sui client objects DEVICE_ADDRESS`). Put
   the DeviceCap ID into `signer.toml` and both IDs into `config.toml`, then
   install them in that order.
4. Give the device a little SUI for status reports, or enroll it in the fleet
   budget during registration.

<a id="check-that-it-is-running"></a>

## Expected result: check that it is running

On Linux:

```sh
systemctl status suifleet.service
journalctl -u suifleet.service -f
```

On macOS, `launchctl print system/org.suifleet.daemon` and
`/var/log/suifleet/daemon.log`. The device shows as online on the Devices page
once it sends its first report. Registration alone does not mean the client is
installed or online.

To recover from a configuration error without losing the device identity or
its history, follow
[troubleshooting without losing state](OPERATIONS.md#troubleshooting-without-losing-state).
Never delete `device.key` or `/var/lib/suifleet` to fix a network or
configuration problem.

<a id="limits"></a>

## Recovery and limits

- No prebuilt binaries, packages or images are published for download yet.
- Physical Raspberry Pi boot and recovery acceptance remain in
  separate physical-target acceptance.
- The generic Linux steps reuse the Yocto recipe's files; they have not been
  tested as a distribution package.
- Public docs hosting and distribution publication are separate from this guide
  build. An operator can configure the console’s `NEXT_PUBLIC_DEVICE_DOCS_BASE`
  to the deployed curated docs root (default `https://docs.suifleet.io/`).
  This setting is a documentation destination, never a package-download promise.

## Technical reference

Integrators can inspect the [runtime configuration reference](../suifleet-edge/config.example.toml) and [deployment manifest protocol](DEPLOYMENT_MANIFEST.md). Engineering references include historical validation; they do not establish physical or production acceptance.
