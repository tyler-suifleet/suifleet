# Debian packages

SF-231 provides local-file `.deb` packaging for amd64 and arm64 GNU/Linux.
The package includes the daemon and the same isolated signing service used by
Yocto. It requires systemd and Linux 6.5 or later (the signer pins callers with
SO_PEERPIDFD); Debian 12 needs a suitable newer/backports kernel. Ubuntu 24.04
has a suitable kernel. Non-systemd source builds remain available; this package
does not change their runtime support.

## Build and availability

No public signed APT repository or published Debian download is available yet.
Build from source or obtain a verified release bundle from your fleet operator.
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
installs, upgrades, removes, reinstalls and purges real packages in disposable
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
