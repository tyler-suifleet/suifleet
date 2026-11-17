# Verify a client release bundle

Authenticate the publisher, target and contents of a client bundle before installation. A checksum alone does not identify its publisher.

## Prerequisites

Obtain a complete signed bundle from your fleet operator and public trust roots through an authenticated channel. Use an already trusted verifier; do not trust an unverified binary merely because it can verify itself. No public signed download or APT repository is promised here.

## Steps

1. Check that the bundle contains `release.json`, `release.sig.json`, its target archive, `sbom.spdx.json` and `provenance.json`. Debian bundles also include their signed package/checksum entries.
2. With the trusted verifier, verify the bundle and exact target (example for Linux arm64):

```sh
suifleet --verify-release /path/to/bundle /etc/suifleet/release-trust.toml aarch64-unknown-linux-gnu
```

3. Confirm the expected reviewed commit and target in the signed manifest. Install only after verification exits successfully.
4. Follow the platform's installation/configuration steps. Keep public trust roots root-owned. Private release signing keys never belong on devices or in images.

## Expected result

Verification accepts at least one trusted non-revoked signature, every listed file's size/hash, and the specified target. The verifier exits nonzero on failure.

## Recovery and limits

Stop on a wrong target, signature or digest; obtain a corrected authenticated bundle. Do not disable verification. Trust rotation requires authenticated public-root delivery before retiring an old key. Protected production signing and distribution acceptance remain separate from reproducible local build evidence.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](RELEASES.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
