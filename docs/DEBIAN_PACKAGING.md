# Debian packages

SF-231 provides local-file `.deb` packaging for amd64 and arm64 GNU/Linux.
The package includes the daemon and the same isolated signing service used by
Yocto. It requires systemd and Linux 6.5 or later (the signer pins callers with
SO_PEERPIDFD); Debian 12 needs a suitable newer/backports kernel. Ubuntu 24.04
has a suitable kernel. Non-systemd source builds remain available; this package
does not change their runtime support.

## Build and availability

Obtain a [verified release bundle](CUSTOMER_RELEASES.md) from an approved GitHub
release or your fleet operator. An operator can also host its signed APT snapshot.
Production publication remains pending under [SF-042](https://github.com/tyler-suifleet/suifleet/issues/62).
The release builder produces a `.deb`, checksum, SPDX SBOM and provenance for
each amd64/arm64 target. These files are included in the signed `release.json`;
protected release signing and public publication remain subject to
[release setup](RELEASES.md) and SF-042/#62.

```sh
python3 scripts/release.py build --target x86_64-unknown-linux-gnu --out dist-amd64 --reproduce
python3 scripts/release.py build --target aarch64-unknown-linux-gnu --out dist-arm64 --reproduce
```

Use the pinned `suifleet-release:local` image built from
`docker/release.Dockerfile`. Builds use committed source only. Package versions
are `<crate-version>-1+git<commit12>`; architectures are `amd64` and `arm64`.
For local development with an already built ELF64 GNU binary:

```sh
python3 scripts/build_deb.py --binary suifleet-edge/target/release/suifleet \
  --arch amd64 --version 0.2.2-1 --epoch 1793606400 --out dist-deb
```

This latter command creates an unsigned package and SHA-256 file, not a signed
release or SBOM. It rejects a binary for the wrong architecture. Do not package
binaries built against a newer libc than the declared minimum (2.35); use the
pinned release builder for distribution.

## Install, configure and start

Verify the complete signed bundle first using a previously trusted verifier and
trust roots ([RELEASES.md](RELEASES.md)); checksums alone do not authenticate a
publisher. Then install the matching local file:

```sh
sha256sum -c suifleet_*.deb.sha256
sudo apt install ./suifleet_0.2.2-1+gitCOMMIT_amd64.deb
```

Replace the filename with the actual version/architecture (`dpkg --print-architecture`).
APT resolves dependencies from your distribution. The package installs no device
keys, enrollment files or fleet configuration. It creates separate system users
`suifleet` and `suifleet-signer`, `/etc/suifleet` (root:suifleet 0750) and
`/var/lib/suifleet` (suifleet:suifleet 0700). Templates are in
`/usr/share/doc/suifleet/examples/`. No services are enabled or started, including
on upgrade. No reboot, enrollment or wallet spending is performed.

Generate a unique local identity explicitly (offline), then follow
[the install guide](INSTALL_DEVICE_CLIENT.md#signing-service-configuration-signertoml)
to install the manifest, signer configuration and daemon configuration in that
order. Use service signing, not a daemon-readable legacy key, for isolation:

```sh
sudo systemctl start suifleet-provision.service
sudo cat /etc/suifleet/device.address
# Register/enroll explicitly, install deployment.json, signer.toml, config.toml.
sudo systemctl restart suifleet-provision.service
sudo systemctl enable --now suifleet-signer.socket suifleet-enrollment.path
sudo systemctl start suifleet.service
```

The provisioning helper preserves existing keys and changes their ownership to
`suifleet-signer` when service signing is configured. The signer socket is
suifleet-signer:suifleet 0660; components run as `suifleet`, never root. Firmware
helpers are not installed by this package. Consult [key isolation](KEY_ISOLATION.md)
for policy and kernel requirements.

## Signed APT snapshots

Each immutable release includes `suifleet-VERSION-apt.tar.gz`, a static flat
repository with `InRelease`, detached `Release.gpg`, `Packages` and the
same Debian packages included in the signed client bundles. APT verifies the
OpenPGP repository signature and the package SHA-256 from its signed index.
The Debian packages use repository authentication through APT. For standalone
files, verify the signed client bundle instead.

Operators stage the archive in a new directory, verify its signatures against
the separately authenticated publisher keyring and check package digests
against the signed per-target client bundles. For example, from the snapshot
directory run:

```sh
gpgv --keyring /path/to/authenticated-publisher.gpg InRelease
gpgv --keyring /path/to/authenticated-publisher.gpg Release.gpg Release
```

Serve the complete directory over
HTTPS at a version-specific path and expose it only after staging all files.
Keep prior snapshots for pinned fleets, publish new versions at new paths, and
restrict writes to the publication role. Rotate the OpenPGP key by delivering
the new scoped public keyring to clients before issuing new snapshots. Keep
old trust available only as long as old snapshots are still approved.
Clients explicitly select a snapshot and package version. There is no moving
channel or automatic freshness claim. Move the URL and pin together via fleet
configuration management.

Have your operator supply both the snapshot URL and the exact Debian package
version. Install the authenticated binary OpenPGP keyring as
`/etc/apt/keyrings/suifleet.gpg` (root-owned, mode 0644). Example source in
`/etc/apt/sources.list.d/suifleet.sources`:

```text
Types: deb
URIs: https://packages.example.org/suifleet/v0.2.3/
Suites: ./
Architectures: amd64
Signed-By: /etc/apt/keyrings/suifleet.gpg
```

Substitute your operator's URI and `amd64` or `arm64`. In
`/etc/apt/preferences.d/suifleet`, set the exact approved version, including
the Git suffix:

```text
Package: suifleet
Pin: version 0.2.3-1+gitCOMMIT
Pin-Priority: 990

Package: suifleet
Pin: version *
Pin-Priority: -1
```

Run `sudo apt-get -o APT::Update::Error-Mode=any update`, check
`apt-cache policy suifleet` for the expected source and candidate, and run
`sudo apt-get install suifleet=APPROVED_VERSION`. For an upgrade, move the
source and pin to the approved new snapshot, re-run the update/check, then run
`sudo apt-get install --only-upgrade suifleet=APPROVED_VERSION`.
Equal crate versions with distinct Git suffixes do not encode chronological
ordering. Use an incremented crate version for a supported upgrade and check
with `dpkg --compare-versions NEW gt OLD` before rollout.

After removal or purge, remove the APT source, pin and scoped keyring if the
device will no longer use the repository. The package's runtime state is
preserved as described below.

## Upgrade, removal and purge

`sudo apt install ./NEW.deb` stops running units before replacing binaries.
After reviewing configuration, explicitly start the units again as above.
Operator files are never shipped as defaults or overwritten. Keys, address,
configuration, enrollment data and journals remain intact on upgrade, ordinary
`sudo apt remove suifleet`, and even `sudo apt purge suifleet`. Purge removes the
packaged trust-root conffile and service enablement bookkeeping; it deliberately
retains runtime data and user IDs to avoid identity loss or UID reuse. Reinstall
reuses the accounts and state. Back up identity securely before any deliberate
manual decommissioning; package scripts never recursively delete runtime data.

## Verification

Run `python3 scripts/test_deb.py --packages DIR` on a Docker host. The harness
rejects unknown APT signing keys and modified packages, then installs, pins,
upgrades, removes, reinstalls and purges real packages from signed snapshots in disposable
Debian 12 and Ubuntu 24.04 containers for both architectures (ARM via QEMU when
needed), checking identity/config/journal preservation and unprivileged CLI
operation. For native amd64 systemd/cgroup isolation, signed simulator reports
and live package upgrade, build `grpc_sim_shim` from the same source and run:

```sh
python3 scripts/test_deb.py --packages DIR --arch amd64 --systemd \
  --shim /absolute/path/to/grpc_sim_shim --evidence /tmp/debian-smoke.json
```

The privileged disposable systemd containers mask `systemd-binfmt.service` so
they cannot reset the host's emulation registrations. ARM64 CLI/signer and package
lifecycle coverage uses QEMU on this host; it is not native ARM systemd evidence.
Container results do not establish physical target acceptance.
