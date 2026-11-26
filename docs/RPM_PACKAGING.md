# SuiFleet RPM packaging

The initial profile is Rocky Linux 10 x86_64 with systemd and a kernel that
supports `SO_PEERPIDFD` (Linux 6.5 or newer). See [key isolation](KEY_ISOLATION.md).
Older enterprise kernels require the portable signer in SF-292 ([#818][portable]).
Package lifecycle tests share the host kernel. Test results identify the
kernel and userspace used. Public repository publication remains a protected
deployment step under SF-042 ([#62][release]).

## Build

Build a trusted x86_64 GNU/Linux bundle from committed source using the pinned
[release builder](RELEASES.md). Use a Rocky Linux 10 RPM tools image and keep
its immutable image ID for reproducibility.

```sh
python3 scripts/release.py build --target x86_64-unknown-linux-gnu --out dist-client --reproduce
docker build --platform linux/amd64 -f docker/rpm.Dockerfile -t suifleet-rpm:local docker
mkdir dist-rpm
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD:/src:ro" -v "$PWD/dist-rpm:/out" suifleet-rpm:local \
  python3 /src/scripts/build_rpm.py --bundle /src/dist-client --out /out
```

Use the same reviewed source checkout for both steps. The package uses the
release ELF unchanged and carries the upstream SPDX SBOM and RPM provenance
as sidecars. The provenance ties the RPM to the upstream build manifest,
source commit, RPM spec and payload hashes. Build the unsigned RPM twice and
compare the output files with the same tool image and inputs. Increase
`--release` for subsequent builds of the same client version. OpenPGP signing
is a separate operation with its own signing time.

## Sign and stage

Use a protected release operator environment with the approved RPM signing
key already in the GnuPG keyring. Verify the upstream bundle against its
trusted source or signed release manifest before packaging. Use the reviewed
RPM directory from the reproducible build as input.

```sh
python3 scripts/prepare_rpm_repo.py --package dist-rpm/suifleet-VERSION-RELEASE.x86_64.rpm \
  --fingerprint "$APPROVED_PRIMARY_FINGERPRINT" --out staged-rpm
```

Publish a completed snapshot only after independent verification. Configure
DNF with both `gpgcheck=1` and `repo_gpgcheck=1`. Verify the published key
fingerprint through an independent trusted channel before importing it or
accepting DNF's key prompt. Sign and verify the repository manifest to
authenticate the SBOM/provenance alongside the RPM. Production key setup,
hosting, repository publication and rotation are operator actions.

## Install and upgrade

Install with `dnf install suifleet` from the verified repository. The RPM
creates separate unprivileged daemon and signer accounts. It creates
`/etc/suifleet` (root:suifleet 0750) and `/var/lib/suifleet`
(suifleet:suifleet 0700). The operator provisions and enrolls each device using
the [systemd install flow](INSTALL_DEVICE_CLIENT.md#signing-service-configuration-signertoml).
Templates are in `/usr/share/doc/suifleet/examples/`.

Upgrade with `dnf upgrade suifleet`. The RPM stops the installed units before
replacing the binary. Review configuration and explicitly start the required
units after upgrading. Install and upgrade perform no automatic activation or
enrollment. Use the package manager to update the daemon when isolated signing
is configured. Platform integrations own firmware installation and reboot
decisions.

`dnf remove suifleet` stops and disables units, removes packaged executables
and preserves identities, operator configuration, enrollment, journals and
system accounts. RPM may save modified packaged release trust configuration
as `release-trust.toml.rpmsave`; review it explicitly when reinstalling.
Back up identity securely before deliberate decommissioning.

[portable]: https://github.com/tyler-suifleet/suifleet/issues/818
[release]: https://github.com/tyler-suifleet/suifleet/issues/62

## Package QA

Build release numbers 1 and 2 in separate output directories using the same
bundle. Run on a disposable Docker host with a suitable kernel:

```sh
python3 scripts/test_rpm.py dist-rpm-1/suifleet-*.rpm dist-rpm-2/suifleet-*.rpm
```

The harness uses a privileged, network-disabled Rocky systemd container and a
disposable key. It checks DNF signature enforcement, package installation
and verification, provisioning, signer socket activation, upgrade and
removal. This covers RPM integration on the runner's kernel. Native Rocky
boot and SELinux enforcing policy require separate host validation.
