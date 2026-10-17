# Validation evidence

Checks recorded on 2026-10-13 before committing the local changes. No contracts were published and no hardware was updated during validation.

| Check | Result |
|---|---|
| `sui move test --path contracts` | 13 passed, 0 failed |
| `cargo test --manifest-path suifleet-edge/Cargo.toml` | 10 passed, 0 failed |
| `cargo build --manifest-path suifleet-edge/Cargo.toml` | Passed |
| `cargo fmt --manifest-path suifleet-edge/Cargo.toml` | Applied |
| `python3 scripts/test_ota.py` | All integration scenarios passed |
| `pnpm typecheck` in `dapp/` | Passed |
| `pnpm build` in `dapp/` | Passed; static pages generated |
| `git diff --check` | Passed |
| Python harness AST and publish script Bash syntax | Passed |

Move, Rust and frontend builds used the existing `sui-dev` image: Sui 1.71.1, Rust 1.87.0, Node 22.22.2. The host Rust installation could not run rustc, so the working image was used. The integration harness ran the built Linux daemon against local HTTP simulators and a harmless updater.

## Covered behavior

- Recipe indirection and exact on-chain recipe/artifact digest checks.
- Direct and live group targeting; non-owner creators and deleted objects ignored.
- Bound device signer and target checks; operator impersonation/non-member reports rejected on-chain in Move tests.
- SDK-independent address/signature vector; rejected transaction execution effects.
- Safe enrollment baseline, multi-page events, offline delivery and restart deduplication.
- Atomic journal recovery, single-daemon lock and retry of status reports after restart.
- Unsupported run/shutdown/configuration rejected; hash failures do not invoke the updater.
- Idempotent software provisioning and key file permissions; bounded apply execution.
- Wallet/client SDK type compatibility and production static export.

## Limits

No public-chain transactions, browser-wallet acceptance test, real Walrus certification, Yocto image build, hardware flashing, reboot/power-loss test, rollback, staged rollout, fleet health service or supported-transport migration was performed. The simulator does not prove those behaviors.

Later live evidence supersedes parts of this list. On 2026-10-15, SF-010 ran on Sui testnet with Walrus testnet: the package-upgrade gate (real daemon, independently read receipts and effects), the upgrade-security suite (legacy objects, old-package and impersonation attacks, revocation) and the browser wallet gate (the dApp in headless Chromium with a scripted wallet, including an on-chain abort). See [TESTNET.md](TESTNET.md#sf-010-acceptance-matrix) and [the receipt](validation/sf010-testnet.json). A real extension wallet, Yocto, hardware and rollback remain unvalidated.

Existing Move linter warnings about redundant `public entry` declarations remain. The Next build reports an older Browserslist dataset. These did not fail the checks.

## Migration requirements

Deploy/upgrade the revised contracts before using the revised app/daemon. Preserve original package/type origins, reissue legacy DeviceCaps with signer bindings, and recreate legacy deployments with recipe digests. Devices registered using the old SHA3-derived address need re-enrollment. Do not erase an existing deployment journal to force an update.

The JSON-RPC retirement and remaining feature work are detailed in [the parity audit](GREENGRASS_PARITY_AUDIT.md).
