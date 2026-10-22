# Raspberry Pi 3 Model B+ development SD image

SF-029 builds a 64-bit `raspberrypi3-64` image through BitBake. Physical flashing,
boot, real-device identity persistence, networking and OTA acceptance remain
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32). No device was
programmed by this build. This writable development image is not a production
release or a firmware rollback design. SF-042 remains on hold.

The current signer-service image (SF-066), its checksums and exact verification
are in [PI_IMAGE_REFRESH.md](PI_IMAGE_REFRESH.md). The earlier SF-029 artifact in
[PI_IMAGE_BUILD.md](PI_IMAGE_BUILD.md) is an older public smoke baseline only.

## Rebuild

Use the pinned Scarthgap/Rust-mixin layers
from `kas/packaging.yml` on Ubuntu 22.04. The target is `suifleet-pi-image`, not
the package-only `suifleet` target. Rust crates are fetched/checksummed by
BitBake and compiled frozen/offline. Source is commit
`d616fa16e1d3e90eb0f97a669f693dec99546d3c` (reviewed main with the SF-053
signing service, SF-026 launcher and SF-022 Seal sessions; the SF-029 baseline
used `795de747`); its Cargo.lock SHA-256 is
`310102d73d7644815fe05df24917c332136836d64dcff5f7ca35b44c63b2d937`.
The image includes the cargo-built daemon, first-boot helper, harmless updater,
CA certificates, the BSP kernel modules/udev rules, OpenSSH, systemd-networkd,
systemd-resolved, systemd-timesyncd, iproute2 and ethtool.

The repository image enables only public-key SSH, disables password and root
SSH login, and creates `operator`. It contains no authorized public keys by
default. Before building an accessible image, add a **private local layer** with
a `suifleet-pi-access_%.bbappend` that installs your `authorized_keys` into
`/home/operator/.ssh` (directory 0700, file 0600, owner/group `operator`). Add
that layer to `bblayers.conf` for a manually configured BitBake build. Kas
regenerates that file, so use the private kas overlay shown below for kas builds.
Never put private keys in the layer. Image
assembly fails if the authorized public-key file is absent or malformed. Keep
personalized public keys outside Git. The delivered local build includes the
user's two authorized public keys through this overlay. No password is set.
`operator` has explicit `sudo` access without another password for development;
anyone holding an authorized private key can administer this device. Use this
image on a trusted development LAN.

A private layer needs a `conf/layer.conf` with its own collection, pattern,
priority and `LAYERSERIES_COMPAT` (`scarthgap`), plus this append at
`recipes-core/suifleet-pi-access/suifleet-pi-access_%.bbappend`:

```bitbake
FILESEXTRAPATHS:prepend := "${THISDIR}/files:"
SRC_URI += "file://authorized_keys"
do_install:append() {
    install -d -m 0700 -o operator -g operator ${D}/home/operator/.ssh
    install -m 0600 -o operator -g operator ${SUIFLEET_UNPACKDIR}/authorized_keys ${D}/home/operator/.ssh/authorized_keys
}
FILES:${PN} += "/home/operator/.ssh"
```

Put only the authorized **public** key lines in the adjacent
`files/authorized_keys`. For kas, keep this additional configuration outside Git
and select the private layer's absolute path:

```yaml
header:
  version: 14
repos:
  private-access:
    path: /absolute/path/to/private-access
```

Build with `kas build kas/pi-image.yml:/absolute/path/to/private-access.yml`.
No personalized key file or private overlay belongs in the repository.

The SD image has Raspberry Pi firmware/kernel/DTBs on its FAT boot partition
and an ext4 writable root. The root includes spare working space; larger SD
cards do not automatically enlarge it. Keep state backups before resizing or
reflashing. No build command opens a removable device.

## Flash from Windows/WSL

Use the delivered `.wic.bz2` and SHA-256 file recorded with the build evidence.
Windows can access the WSL Linux artifact directory through
`\\wsl.localhost\<your-distribution>\home\tyler\dev\codex\yocto-sf066\artifacts\`;
`wsl.exe -l -q` gives the distribution name. You can also open the directory
with `explorer.exe .` from WSL. Verify `sha256sum -c SHA256SUMS` in the artifact
directory. Decompress with `bzip2 -dk IMAGE.wic.bz2` if the flashing application
does not accept bzip2 images.

In Raspberry Pi Imager choose the decompressed `.wic` as a custom image.
If the file picker hides `.wic`, rename the decompressed file to `.img`
without changing its bytes. Use a card of at least 2 GB. Explicitly select the intended removable SD card, checking its capacity
and identity. Flashing destroys that selected card's contents. Do not apply OS
customizations that override users, SSH or networking. This task supplies no
`dd` command and guesses no Windows disk or Linux block-device name. Eject the
card after verification; insert it into the powered-off Pi and connect wired
Ethernet before power.

## First boot and access

Hostname is `suifleet-pi`, login is `operator`. Wired Ethernet (`eth*`/`en*`)
automatically obtains its address, router and DNS through DHCP on the router's
actual subnet. There is no static IP. Find the lease named `suifleet-pi` in the
router's DHCP client list; hostname resolution depends on the router and is
not guaranteed. Connect using the assigned address:

```sh
ssh -i ~/.ssh/id_ed25519_suifleet operator@ADDRESS_FROM_ROUTER
cat /usr/share/suifleet/build-info
sha256sum /usr/bin/suifleet /usr/bin/suifleet-test-updater
sudo systemctl status systemd-networkd systemd-timesyncd suifleet-provision
networkctl status
ip address
ip route
resolvectl status
timedatectl status
timedatectl timesync-status
sudo cat /etc/suifleet/device.address
sudo systemctl status suifleet.service suifleet-enrollment.path suifleet-signer.socket suifleet-signer.service
```

Verify the host fingerprint through a trusted console before accepting an
unexpected SSH host key. Host keys and the SuiFleet device identity must be
created at runtime, independently for each fresh card. The unique device key
stays in `/etc/suifleet/device.key`; never display/copy it into logs. The address
is public. The unenrolled daemon stays inactive until valid enrollment is
installed, rather than running against placeholder IDs. The address filename
and enrollment procedure are described in [YOCTO_PACKAGING.md](YOCTO_PACKAGING.md).

Time must synchronize before testnet TLS: `timedatectl status` should report
`System clock synchronized: yes`. The image uses systemd-timesyncd with DHCP
NTP information and its configured fallback servers. If UDP NTP is blocked,
repair the network/time source before chain acceptance; do not disable TLS
verification. Check `journalctl -u systemd-timesyncd` and `date -u`.

Use [the verified manifest enrollment procedure](YOCTO_PACKAGING.md) after
registering the public signer address. Preserve per-type origins and the
supported client/protocol range from that manifest; do not paste stale IDs.
Install `deployment.json` first (root:suifleet 0640), then `signer.toml`
(root:suifleet-signer 0640, from `/etc/suifleet/signer.toml.example`) and
`config.toml` last (root:suifleet 0640, from the service-signer example),
each atomically. Before the daemon starts, provisioning hands the key to
`suifleet-signer`; component commands cannot read it. See
[KEY_ISOLATION.md](KEY_ISOLATION.md). Launcher and signer are modes of
`/usr/bin/suifleet`. Enabled daemon self-update is refused with the service
signer ([DAEMON_UPDATE.md](DAEMON_UPDATE.md)); keep it disabled. No reusable
session credential is installed by this image. For this harmless one-shot
application test use:

```toml
[apply]
command = "/usr/bin/suifleet-test-updater"
mode = "direct"
allow_recipe_commands = false
```

Generate current-schema recipes/artifacts with `scripts/test_component.py` as
shown in [the component guide](../tests/components/suifleet-test-app/README.md).
It supports the current one-shot application path, not managed run/shutdown.
Keep `/etc/suifleet/device.key` and `/var/lib/suifleet` across restart. Never
resolve enrollment failures by deleting identity or journals. No enrollment,
release-trust policy or production credentials are built into this image.

UART is enabled for diagnostics; serial login still requires authentication
and this image has no password account. Initial supported login is wired SSH.
Wi-Fi is optional and is not provisioned. Rebuild with the board firmware and
a deliberately selected supplicant/network manager before using it; then supply
the regulatory domain and credentials through private runtime configuration.
The wired image does not promise an on-device package manager or Wi-Fi setup. Never commit Wi-Fi credentials.
Do not assume UART or Wi-Fi acceptance from the SD build.
