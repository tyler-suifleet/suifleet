# Download and verify a client release

Get the version and reviewed commit for your fleet from your operator. Use the
matching asset from the [GitHub Releases page](https://github.com/tyler-suifleet/suifleet/releases)
when a protected release has been published, or obtain the same signed bundle
from your operator.

| Runtime target | Archive target | Debian package |
|---|---|---|
| Linux amd64 | `x86_64-unknown-linux-gnu` | `amd64` |
| Linux arm64 | `aarch64-unknown-linux-gnu` | `arm64` |
| Linux ARMv7 hard-float (ARM EABIhf, ARMv7-A with VFPv3 and NEON) | `armv7-unknown-linux-gnueabihf` | Archive only |

The Linux binaries require glibc 2.35 or later. Check the **userspace** ABI as
well as the CPU. For example, an arm64 kernel can have 32-bit armhf userspace.
ARMv6, soft-float and musl are outside these release profiles.

1. Download `suifleet-VERSION-TARGET-release.tar.gz`. If available, compare its
   SHA-256 against `SHA256SUMS` to catch transfer errors.
2. For a GitHub download, use a trusted GitHub CLI to verify the outer asset
   against the release workflow, tag and reviewed source commit. Replace
   `VERSION`, `TARGET` and `REVIEWED_COMMIT` with the operator-approved values.

   ```sh
   gh attestation verify suifleet-VERSION-TARGET-release.tar.gz \
     --repo tyler-suifleet/suifleet \
     --signer-workflow tyler-suifleet/suifleet/.github/workflows/release.yml \
     --source-ref refs/tags/VERSION --source-digest REVIEWED_COMMIT
   ```

3. Extract the outer bundle as an unprivileged user into an empty staging
   directory. It contains the target archive, checksums, SPDX SBOM,
   provenance, manifest and detached manifest signature. Debian bundles also
   carry a `.deb` and its checksum.
4. Use a trusted SuiFleet verifier and public trust roots acquired through
   an authenticated channel. You can use the binary from the attested archive
   if your operator approves the GitHub workflow identity for initial download
   trust. Run the Ed25519 bundle check before installing.

   ```sh
   suifleet --verify-release /path/to/staging /etc/suifleet/release-trust.toml aarch64-unknown-linux-gnu
   ```

   Substitute the correct target and confirm the manifest version and reviewed
   commit. Use only files listed in the verified manifest. Stop on a verification
   error and obtain a corrected bundle.
5. Follow the [generic archive install](INSTALL_DEVICE_CLIENT.md#generic-linux)
   or [Debian instructions](DEBIAN_PACKAGING.md). For APT, the separate signed
   snapshot uses a separately authenticated OpenPGP keyring.

Keep public trust roots root-owned. Obtain initial trust roots and their
replacements through the same authenticated operator channel. A checksum alone
cannot authenticate a publisher. Protected signing and actual publication
acceptance are tracked in [SF-042](https://github.com/tyler-suifleet/suifleet/issues/62).
See [release engineering](RELEASES.md) for provenance and rotation.
