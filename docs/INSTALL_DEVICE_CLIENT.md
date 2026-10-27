# Install the device client

The SuiFleet device client is the `suifleet` daemon. It runs on each device,
holds the device's own key, watches the network for deployments aimed at it and
reports results. The console links here from Get started, Devices and
Enrollment (SF-169).

A device needs four things:

1. The `suifleet` binary and its service files.
2. A device key, generated on the device. The client prints its public address.
3. The network manifest `deployment.json` for the fleet's network.
4. A configuration file, `/etc/suifleet/config.toml`, written after the device
   is registered or enrolled.

## Get the client

The client is built from this repository's `suifleet-edge` crate. **No
prebuilt download is published yet.** The repository can produce signed,
reproducible release bundles ([RELEASES.md](RELEASES.md)), but none have been
released for operators to download. Build it yourself for now:

| Platform | How to get it | Status |
|---|---|---|
| Raspberry Pi and other Yocto targets | [Yocto image and recipe](#yocto-image-and-recipe) | Image builds and QEMU boot recorded; physical Pi acceptance pending ([SF-031](https://github.com/tyler-suifleet/suifleet/issues/32)) |
| macOS 15 on Apple silicon | [macOS package](#macos-package) | Validated on a hosted macOS 15 runner, not a physical Mac |
| Other Linux with systemd | [Generic Linux](#generic-linux) | Manual install of the same files as the Yocto recipe; not a tested distribution package |

Windows and 32-bit x86 Linux have their own notes in
[WINDOWS_RUNTIME.md](WINDOWS_RUNTIME.md) and [LINUX_X86.md](LINUX_X86.md).

Never copy a device key between devices or into an image, package or
repository. Each device generates its own at first boot or install.

## Yocto image and recipe

The `meta-suifleet` layer packages the client as the `suifleet` recipe, with
first-boot key provisioning and systemd services. Build a Raspberry Pi 3 Model
B+ SD image with kas 4.7 on Ubuntu 22.04:

```sh
export KAS_WORK_DIR=/absolute/path/to/yocto-work
export KAS_BUILD_DIR="$KAS_WORK_DIR/build-pi-image"
kas build kas/pi-image.yml:/absolute/path/to/private-access.yml
```

The private overlay adds your SSH public key; see [PI_IMAGE.md](PI_IMAGE.md)
for it, flashing and first login. To add only the client to your own image,
add `suifleet-edge/meta-suifleet` to your layers and `suifleet` to the image's
packages; [YOCTO_PACKAGING.md](YOCTO_PACKAGING.md) lists the pinned inputs and
package-only builds.

At first boot `suifleet-provision.service` generates the key, without
networking, and prints the address (see
[Find the device address](#find-the-device-address)). The daemon
`suifleet.service` stays stopped until `/etc/suifleet/config.toml` exists.

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

Service control, logs and limits are in [MACOS_RUNTIME.md](MACOS_RUNTIME.md).

## Generic Linux

On another systemd-based Linux, install the same binary, accounts and units as
the Yocto recipe. From a checkout, with Rust 1.94.0:

```sh
cargo build --locked --release --manifest-path suifleet-edge/Cargo.toml
F=suifleet-edge/meta-suifleet/recipes-edge/suifleet/files
sudo useradd --system --no-create-home --home-dir /var/lib/suifleet --shell /usr/sbin/nologin --user-group suifleet
sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --user-group suifleet-signer
sudo install -m 0755 suifleet-edge/target/release/suifleet /usr/bin/suifleet
sudo install -D -m 0755 "$F/suifleet-provision" /usr/libexec/suifleet-provision
sudo install -m 0644 "$F"/suifleet*.service "$F"/suifleet*.path "$F"/suifleet*.socket /etc/systemd/system/
sudo install -d -m 0750 -g suifleet /etc/suifleet
sudo systemctl daemon-reload
sudo systemctl enable --now suifleet-provision.service suifleet-enrollment.path suifleet-signer.socket
```

Use the recipe's examples as templates: `$F/config.toml.example` and
`$F/signer.toml.example`. They keep the key in the separate signing service
([KEY_ISOLATION.md](KEY_ISOLATION.md)). The privileged firmware path is
optional; see [OPERATIONS.md](OPERATIONS.md#execution-privileges) before
enabling `suifleet-firmware.path`.

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

- The console's **Download deployment.json** button saves the manifest this
  console was built with.
- Enrollment media downloaded from the Enrollment page already include it.
- A publisher's release directory `deployments/ENV/current/` contains it
  ([DEPLOYMENT_MANIFEST.md](DEPLOYMENT_MANIFEST.md)).

Install it first, as `/etc/suifleet/deployment.json`, root-owned. On Linux use
group `suifleet`, mode 0640.

## Configuration file

`/etc/suifleet/config.toml` names the manifest, the device's on-chain IDs and
how updates are applied. Install it **last**: on Linux, its appearance starts
the daemon. A minimal Linux configuration with the signing service:

```toml
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
Write those IDs into `config.toml`, then install it. Details, claim funding and
security are in [ENROLLMENT.md](ENROLLMENT.md#claim-enrollment-and-bulk-onboarding).

## Register by address

Without Enrollment, register one device at a time:

1. Read the device address as above.
2. In the console, use Get started, or Devices → Register by address. Paste the
   address and choose the architecture. Your wallet approves the transaction.
3. Copy the Device object ID from the Devices page (show technical details).
   Registration sends the DeviceCap to the device address, so read its ID from
   the registration transaction's created objects in a Sui explorer, or list
   the objects that address owns (`sui client objects DEVICE_ADDRESS`). Put
   both IDs into `config.toml`, then install it.
4. Give the device a little SUI for status reports, or enroll it in the fleet
   budget during registration.

## Check that it is running

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

## Limits

- No prebuilt binaries, packages or images are published for download yet.
- Physical Raspberry Pi boot and recovery acceptance remain in
  [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32).
- The generic Linux steps reuse the Yocto recipe's files; they have not been
  tested as a distribution package.
- These links point to repository documentation until the documentation site
  (SF-172) exists.
