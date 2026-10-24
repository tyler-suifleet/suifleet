# SF-121 device-local Quilt implementation evidence

Codex, configured/running model `gpt-6.1-sol` (no finer build identifier exposed),
continued the existing claim/session and branch. Implementations are committed
with Tyler Bush as author and committer. No new issue/model worker was created.
Main `b9e8053f4f034ca725686d19870cde6f3f756d63` was integrated in `222bc23`.

## Implemented behavior

The existing daemon forms multi-source/record-chunk Quilts with independent native
signatures/Seal encryption, an authenticated encrypted item manifest and one
fleet-owned storage lifecycle. Formation is journaled before codec execution;
restart reuses exact ciphertext, registration/certification and anchor intents.
Bounded retries/age retain the pending batch for reconciliation. Selected sources
retire only after independently verifying the required chain anchor.

The producer permits at most 16 data items plus a manifest and 4 MiB resulting
Quilt, with a regular blob for a lone useful item. Readers bound chain parent size,
index/header lengths and column ranges before allocation, authenticate each patch,
and preserve ordinary blobs, compact history, shadows and current authority checks.
The reviewed producer/reader profile is QuiltV1/RS2/1000 shards without tags;
unsupported profiles refuse publication/readability, rather than guessing geometry.
No collector, extra server, cross-device aggregation or health/receipt removal.

## Current-main local checks

- `CARGO_BUILD_JOBS=2 CARGO_TARGET_DIR=/home/tyler/.local/state/suifleet/sf121-build cargo +1.94.0 test --locked --manifest-path suifleet-edge/Cargo.toml`: **420 passed, 4 pre-existing ignored**,0 failures after main integration.
- Same target/toolchain `cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bin suifleet --example grpc_sim_shim`: passed.
- `python3 scripts/test_fleet_data_ota.py --binary .../debug/suifleet --shim .../debug/examples/grpc_sim_shim --compact-history --quilt`: passed after integration, including SF-130 fleet settings, mixed regular/Quilt archives, native Seal/browser verification, corrupted spool, interrupted formation, restart, prepaid exhaustion, lost registration response, certificate refresh, uncertain/pruned effects, diagnostics/crashes and shadow changes. The simulator does not verify a real Walrus certificate.
- `/tmp/sf121-tools/sui move test --path contracts/fleet_data --build-env testnet`: **21/21 passed**, pinned compiler 1.81.1, current merged dependencies.
- `python3 scripts/test_protocol_localnet.py --fleet-data-upgrade` in disposable pinned network-none container: passed. Existing-object bytes and type origins preserved; diagnostic API works on existing writer; old/new package entry points reject a revoked device. [Current-main effects/origins](sf121-quilt-main-upgrade.json). The first container invocation lacked the harness's `/home/dev/.move` cache mount; the corrected invocation passed, with no test suppression.
- Node 22.22.2 `node_modules/typescript/bin/tsc --noEmit` and `node_modules/next/dist/bin/next build`: passed after integration; static production export completed.
- `scripts/test_archive_quilt.cjs`: passed actual pinned SDK encoding/patch interoperability with the bounded parser at 3/9/17 items and varying sizes; malformed index/patch lengths, counts, identifiers, quotas and missing/wrong data refused.
- `scripts/test_fleet_bulk.cjs`, `scripts/test_compact_archives.cjs`, `scripts/test_fleet_recovery.cjs`, `scripts/test_shadow_documents.cjs`: passed in this continuation. Archive tests and compact checks also reran on the merged base; 4 MiB chain-parent rejection occurs before transport.
- `scripts/test_quilt_fixture_signer.cjs .../examples/sign_quilt_fixture`: passed real personal-signature and foreign chain/fleet/device/signer/schema/content refusals using a disposable local key; no network transaction.
- `scripts/build_walrus_codec.cjs /tmp/sf121-quilt-codec-bounded`: passed with Node 22.22.2, reviewed esbuild 0.28.2, Walrus 1.2.35 / Sui 2.36.0 and pinned WASM. No new Yocto image or physical acceptance is claimed.
- `python3 scripts/check_archive_envelope.py`: passed effect-derived transaction, byte, fee and allowance arithmetic.
- `python3 -m unittest discover -s scripts/tests -p test_tracking.py` and `python3 scripts/bootstrap_tracking.py`: **10/10 passed**,116 prepared issues, offline/no remote mutation. Preserve current main records plus the already-published SF-121/SF-082 restoration.
- Before integration: 400 Rust tests passed / 4 existing ignored; targeted fleet-data 8/8 and Quilt 3/3; scoped PKCS#11 Quilt 3/3 and updated archive-signing 1/1 passed. These are recorded at their actual source snapshot, not mislabeled new merged-head PKCS#11 runs.

## Public testnet evidence

[Executed effects, source hashes and independent readback](sf121-quilt-runtime.json)
record the **actual pinned SDK 1.2.35 / Sui 2.36.0** codec/browser path on chain
`4c78adac`. Two signed, independently encrypted synthetic items and a manifest
were registered, storage-node written, certified and compact anchored. Owner and
support reads succeeded; a fresh support session received `NoAccessError` after
role revocation while owner reads still succeeded; support was restored.
Wrong/missing patch controls were refused. The actual SDK patch reader independently
matched the bounded reader. Current merged codec preparation reproduced the same
registered blob without another purchase. All 8 effects were independently reread.

The public fixture signs only bounded synthetic messages/sessions through the
explicit-wallet native bridge; operator keys never enter Node. Native daemon
signing/encryption/recovery is separately established by simulator, not called a
public daemon rollout. CLI personal-signing invocations initially refused before
upload; they spent no transaction gas. Setup effects and every earlier failed or
uncertain reservation remain in the original ledger. **393,465,439 / 500,000,000 MIST**
charged allowance; no reset, faucet or package publication in this Quilt probe.
The shared network lock was released after mutations. Read-only revalidation
required no network mutation lease.

## Costs, migration and remaining gates

[Cost analysis](../business/SF121_ARCHIVE_COSTS.md) and
[28-day operating envelope](sf121-operating-envelope.json) explicitly separate
ciphertext/padding/encoded bytes, storage deposits/cleanup, cadence choices,
health, retries and deployment receipts. Equal-cadence packed archives and Quilts
have identical transaction counts and no measured Sui saving. Weekly optional
logs plus daily authenticated health project 40,000 transactions / 1,000 devices / 28 days,
with explicit freshness, volume, retention/capacity and owner-maintenance limits.
The cash reference is scenario arithmetic from testnet effects, not a fleet load
test, mainnet invoice or proof of a $100 subscription margin. SF-128 v3 removes the
older v2 protection scan/slot ceiling; this finite fixture does not measure v3 fees.

Enable `quilt=true` only with the updated codec/checksum, reader and isolated
signer policy. Existing configs stay off; `compact_history` remains a separate
explicit opt-in requiring the additive package and verified origins. No public
Move layout changes are needed for the item format. SF-139 owns opaque component
intake/stream metadata and reuses these ciphertext descriptors/manifest/transport
after merge. Do not build a second publisher.

Current-source CI/automatic testnet checks and fresh independent source/security
review remain coordinator merge gates. Pending hosted CI is not approval and was
not watched. Physical target, actual mainnet and final production-provider/manual
wallet acceptance remain unrun in [#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [#159](https://github.com/tyler-suifleet/suifleet/issues/159). No deployment,
production credentials, mainnet write, physical programming or final pricing approval.
