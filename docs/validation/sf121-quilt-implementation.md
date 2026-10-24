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
Persisted, rate-limited retries retain the pending batch for reconciliation without
permanent failure-count or offline-age exhaustion. Selected sources
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

## PR #253 independent-review repairs (2026-10-24)

The feedback on `f3f3963b8917064a3aaa8c6f2a70c24a2ce52595` identified two
implementation defects and one overstated invariant. The owner branch already
contains snapshot/current main `b9e8053f4f034ca725686d19870cde6f3f756d63`;
`git merge --no-edit origin/main` confirmed it was up to date.

- Fleet chain-summary service degradation keeps the established two-minute sustain threshold,
  independently of signed health reporting cadence. The snapshot regression
  exercises minute/hour/day reporting at 119,999/120,000/180,000 ms for both full
  pub/sub queues and disconnected MQTT bridges. Cadence metadata remains intact;
  the legacy HTTP telemetry path keeps its two-heartbeat-interval judgment,
  checked independently in the same regression.
- Quilt failures use a saturating counter and persisted exponential backoff,
  capped at one hour, with no terminal attempt-count or wall-clock age gate.
  Backoff/idle ticks do not increment failures. A retry deadline is persisted
  before publication work so a crash cannot turn retries into a tight loop.
  Exact ciphertext, purchase and signing identities remain durable. Corruption,
  revoked keys and unknown effects still fail closed; ring overflow remains
  explicit while storage is unavailable. Existing pending journals keep the same
  fields/checkpoints and resume under the new retry policy without a migration.
- Compact history deliberately retains only its head, not an unbounded seen-blob
  set. The corrected contract comment, operating documentation and A→B→A Move
  regression make this limitation explicit. The reader's sequence binding refuses
  an old ciphertext linked at a later sequence; adjacent reuse still aborts.

Fresh checks for these repairs:

- Rust 1.94.0 `cargo test --locked --manifest-path suifleet-edge/Cargo.toml` with
  `CARGO_BUILD_JOBS=2 CARGO_TARGET_DIR=/home/tyler/.local/state/suifleet/sf121-build`:
  **422 passed, 4 existing ignored**, zero failed. Same toolchain/target
  `cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples`:
  passed.
- `/tmp/sf121-tools/sui move test --path contracts/fleet_data --build-env testnet`:
  **22 passed**, zero failed, pinned compiler 1.81.1.
- `scripts/test_fleet_data_ota.py --binary .../debug/suifleet --shim
  .../debug/examples/grpc_sim_shim --compact-history --quilt`: passed with the
  actual daemon, native Seal encryption/signing, shim and browser verifier.
  The new case restarts an eight-day-old batch with 32 prior failures, observes
  failure 33 and hourly backoff, verifies idle ticks preserve the counter, then
  recovers certification and the verified anchor with one registration and the
  original ciphertext descriptors. The fixture advances only persisted retry
  deadlines while the daemon is stopped rather than waiting hours; it does not
  reset the failure count or delete signing checkpoints.
- All 26 post-build OTA harness commands from make test-ota passed against the final rebuilt binaries in a disposable network-none suifleet-dev:sf037 container. The driver ran each exact Makefile command with check=True, as dev except test_supervisor.py as root. Initial host supervisor /proc permissions and an initial all-root CloudWatch adapter refusal were environment errors; the complete final run used the required per-harness users, without changing or ignoring tests.
- `python3 scripts/test_protocol_localnet.py --fleet-data-upgrade` in a disposable
  `suifleet-dev:sf037` container with `--network none`: passed. Existing objects,
  type origins and old/new revoked-device refusal were exercised on local chain
  `9dd26ddd`. [Effects, origins and checks](sf121-feedback-upgrade.json).
  Corrected container mounts for the pinned transaction CLI, shared Git metadata
  and Move cache before the successful run; initial harness setup failures were
  not suppressed or counted as passes.
- Node 22.22.2 frontend `node node_modules/typescript/bin/tsc --noEmit` and
  `node node_modules/next/dist/bin/next build` in `suifleet-dev:sf037`: passed,
  including static production export.
- `cargo build --locked --manifest-path suifleet-indexer/Cargo.toml` with the
  same Rust 1.94.0 target: passed. Final-daemon/indexer
  `python3 scripts/test_service_health.py` in a disposable root network-none
  container: passed. All eight alerts were derived from signed reports,
  sustained failures degraded the device at legacy heartbeat timing, recovery
  resolved the alerts, privacy checks passed and the dApp verified all nine
  envelopes. This verifies that fixing daily chain summaries preserves the
  existing HTTP telemetry behavior.
- `node scripts/test_compact_archives.cjs`, `test_archive_quilt.cjs`,
  `test_fleet_data.cjs`, `test_fleet_bulk.cjs`, `test_fleet_recovery.cjs` and
  `python3 scripts/check_archive_envelope.py`: passed.

Hosted exact-head CI, automatic public-testnet status and fresh independent
review remain coordinator gates, not asserted here. This repair made no public
network write. Earlier public Quilt effects remain historical evidence above.
Physical targets, actual mainnet and final production-provider/manual-wallet
checks remain deferred and unrun in #32/#159. No deployment, hardware programming,
production credentials or final subscription economics are claimed.

### Follow-up: reject partial codec encoding before retry persistence

The review of `dab4c4791fd7154608c0609a384fb53fea07622c` found that the
publication failure handler could persist partially assigned encoding fields.
Encoding response validation now completes before assigning any pending identity
or size. Missing Quilt size, missing/non-string/empty root, and zero/oversize
encoded length fail without changing those fields. The restart regression saves
the failed attempt, reloads the journal, verifies that preparation is still fresh
and source records remain pending, then accepts a corrected response. This avoids
both the manifest-size purchase fallback and a permanently wedged partial root.
No schema, chain API, source acknowledgement or signing-checkpoint change.

Fresh repair checks (Rust 1.94.0, `CARGO_BUILD_JOBS=2`,
`CARGO_TARGET_DIR=/home/tyler/.local/state/suifleet/sf121-build`):

- `cargo +1.94.0 test --locked --manifest-path suifleet-edge/Cargo.toml`:
  **423 passed, 4 existing ignored, zero failed**, including the new malformed
  preparation/persistence/restart regression.
- `cargo +1.94.0 build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples`:
  passed.

Hosted CI and automatic testnet acceptance must be checked by the coordinator on
the pushed head; this repair does not poll them or grant merge approval. Physical,
mainnet and final production-provider/manual-wallet checks remain deferred and
unrun in #32 and #159. Earlier evidence above is historical, not a fresh rerun.

- Fresh repair simulator: `python3 scripts/test_fleet_data_ota.py --binary
  /home/tyler/.local/state/suifleet/sf121-build/debug/suifleet --shim
  /home/tyler/.local/state/suifleet/sf121-build/debug/examples/grpc_sim_shim
  --compact-history --quilt`: **passed**, with real rebuilt daemon, native
  encryption/signatures and browser verification; outage/backoff/restart,
  certification recovery, tamper/replay, diagnostics, shadows and cadence controls.
- `git diff --check`: passed. Fetched `origin/main` remains
  `b9e8053f4f034ca725686d19870cde6f3f756d63`; merge reported already up to date.
