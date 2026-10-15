# Signed daemon releases

SF-039 gives every SuiFleet daemon release three properties:
- **Reproducible:** two independent builds of the same commit are byte-identical.
- **Inventoried:** each release carries an SPDX SBOM of exactly what is linked in.
- **Signed and verified on the device:** each device checks the signature against its own trust roots before installing. This is on top of the content hashes that deployments already authenticate on chain.

## What a release bundle contains

| File | Contents |
|---|---|
| `suifleet-<version>-<commit12>-aarch64-unknown-linux-gnu.tar.gz` | `suifleet/bin/suifleet`, `LICENSE`, `config.example.toml`. Sorted entries, fixed mtime (the commit time), `root:root`, gzip without name or timestamp. |
| `sbom.spdx.json` | SPDX 2.3: the daemon (with its binary's SHA-256) and every package in its normal dependency graph for aarch64, exactly as `cargo tree -e normal --target aarch64-unknown-linux-gnu` resolves it. Each package has its Cargo.lock SHA-256, declared license and purl. Build-script-only dependencies are not linked into the binary and are not listed. |
| `provenance.json` | Commit, `SOURCE_DATE_EPOCH`, target, `rustc -vV`, cargo and linker versions, `Cargo.lock` SHA-256, builder image ID and binary SHA-256 |
| `release.json` | Name, version, commit, target, and the size and SHA-256 of the three files above. This is what gets signed. |
| `release.sig.json` | One or more `{key_id, signature}`: ed25519 over `suifleet-release-v1\n` followed by the bytes of `release.json` |

## Building

```bash
make release-artifacts            # builds HEAD twice; fails unless byte-identical; output in dist/
```

**How it builds:**
- `scripts/release.py` exports the **committed** tree with `git archive`, so uncommitted files never enter a release.
- It builds inside the pinned release image (`docker/release.Dockerfile`).
- Embedded source paths are remapped, so the build location doesn't leak into the binary.

**Builder pins:** Ubuntu 22.04 digest; Rust 1.94.0 via a checksum-verified rustup-init 1.29.1; `gcc-aarch64-linux-gnu` 4:11.2.0-1ubuntu1 (gcc 11.4.0-1ubuntu1~22.04.3cross1); `binutils-aarch64-linux-gnu` 2.38-4ubuntu2.12; `libc6-dev-arm64-cross` 2.35-0ubuntu1cross3.

The daemon uses rustls for both gRPC and Walrus HTTP, so the release links no OpenSSL. Bump these pins deliberately, and record the new versions here.

## Trust roots, signing and rotation

The trust roots are the **public** keys in `release/trust.toml`. Devices install this file root-owned as `/etc/suifleet/release-trust.toml`. Private keys never enter the repository, the device image, or pull-request CI.

```bash
cargo run --example sign_release -- generate /secure/path/release-2026-10.key   # prints the public key
cargo run --example sign_release -- public   /secure/path/release-2026-10.key
cargo run --example sign_release -- sign     /secure/path/release-2026-10.key release-2026-10 dist
```

- **Add a key:** add a `[[keys]]` entry (`id`, `public_key`, `revoked = false`) to `release/trust.toml` and ship it to devices.
- **Rotate:** add the new key and sign releases with **both** keys (`sign` runs once per key and keeps other signatures). Once every device has the new trust file, set `revoked = true` on the old key. From then on, a release signed only by the old key is refused.
- **Revoke a compromised key:** set `revoked = true` and ship the trust file. That key's signatures are ignored. Re-sign current releases with a trusted key.

## Verifying on a device

```bash
suifleet --verify-release /path/to/bundle /etc/suifleet/release-trust.toml
```

It exits non-zero unless both of these hold:
1. At least one signature verifies (strict ed25519) under a trusted, non-revoked key.
2. Every file listed in `release.json` is a regular file inside the bundle (no paths, symlinks or hidden names) with the signed size and SHA-256.

Installers (for example a daemon self-update recipe, or the image build) must run this before installing a release. A deployment's on-chain artifact SHA-256 proves *which* bytes the operator chose; the release signature proves *who built them*.

## CI

`.github/workflows/release.yml` runs only on `v*` tags or manual dispatch, never for pull requests.

1. **Build job:** no secrets, read-only token. It builds the bundle twice from the commit, requires identical output, and uploads it unsigned.
2. **Preflight:** fails closed unless the `release` environment has a required reviewer.
3. **Sign job:** runs in the protected `release` environment.
   - It writes the `RELEASE_SIGNING_KEY` secret (hex seed) to a private temporary file, signs as `RELEASE_SIGNING_KEY_ID`, and verifies the result with the daemon's verifier against `release/trust.toml`.
   - It adds GitHub (Sigstore) build-provenance attestations for every file.
   - For tags, it publishes a GitHub release.
   - With no trusted keys committed, verification fails, and so does the release.

**One-time setup** (by a repository admin):
1. Create the `release` environment with a required reviewer.
2. Add the `RELEASE_SIGNING_KEY` secret and the `RELEASE_SIGNING_KEY_ID` variable.
3. Commit the matching public key to `release/trust.toml`.

## Verified locally (2026-10-14)

- **Reproducibility:** `scripts/release.py build --reproduce` produced byte-identical bundles across two clean builds (4 files).
- **SBOM:** its 181 packages exactly match `cargo tree -e normal --target aarch64-unknown-linux-gnu`, and every registry package has a checksum.
- **Verifier:** Rust tests cover a trusted signature, a tampered artifact (hash and size), a tampered manifest, untrusted and revoked signers, rotation with two signatures, unsafe paths, duplicate trust IDs, and domain separation.
- **End to end on the real bundle:** with a generated key, the real bundle verified. A flipped artifact byte, an edited manifest, and an untrusted key claiming a trusted ID were each rejected. With rotation, A revoked plus B trusted was accepted, and a bundle signed only by the revoked key was refused.

**Not yet done:**
- The release workflow has not run, because it needs the environment and secret above.
- The aarch64 binary has not run on hardware (SF-029/SF-031).
- The Yocto image does not yet install the trust file (SF-025/SF-029).

The protected signing environment, provisioned trust roots, the first release workflow run (including rejection without a protected environment) and Pi trust delivery are tracked as external acceptance in [#62](https://github.com/tyler-suifleet/suifleet/issues/62) and [#32](https://github.com/tyler-suifleet/suifleet/issues/32). Merging this implementation is not evidence that they passed.
