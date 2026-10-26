# SF-127 current-main integration evidence

Implementation merge: `73e95bff`, parents `3d645580f286702bc2cd38c68c01ec7d947c073c`
and current main `b819cb2010ac4890e4d2d3bb5dad62b9df9b6a8d`. This records
software validation on 2026-10-25, not public-network or physical acceptance.

## Authority integration

Managed receipts retain generation 3 and their existing type origins/layouts.
Sharded receipts use generation 4. The frozen legacy map cannot be written through
still-callable managed-v3 package reports: both freeze and finished-state attacks
were submitted and their abort effects checked on the isolated chain. Existing
`_v3` shard API names and `reported_v3` field are retained for ABI compatibility.
Managed reports use new `customer::report_status_v4`/`report_group_status_v4`, with
current customer/fleet/deployment/device membership and active-cap checks. The
unscoped shard routes reject managed records. Managed migration and deletion keep
administrator authority, relocated catalog bindings, rollout extensions and
bounded shard cleanup. Daemon, signer, frontend and indexer distinguish the routes.

## Verified checks

Commands below ran in disposable network-none containers with the pinned Sui
1.81.1, Rust 1.94.0, Node 22.22.2 and pnpm 11.0.8 toolchain. The full frontend
check additionally used Chrome for Testing 156.0.8078.4 and the checksum-verified
Sui transaction CLI 1.82.1. No testnet wallet or shared network lease was used.

- `sui move test --path contracts`: **276 passed**.
- `sui move test --path contracts/fleet_storage`: **65 passed**.
- `sui move test --path contracts/fleet_data --build-env testnet`: **38 passed**.
- `python3 scripts/check_move_security.py`: **passed** strict compiler/linter
  gates, reviewed unsuppressed baseline and deliberately failing authority
  mutations. Its unit snapshot had 274 tests; the final two managed
  catalog/rollout cleanup tests passed in the 276-test run above.
- `python3 scripts/check_move_native_vm.py`: **19 passed**.
- `python3 scripts/check_move_proofs.py --tools /proof-tools`: **passed 63
  positive obligations and all 27 negative controls**, including managed receipt
  version, future-version rejection, no-upgrade and clear-history mutations.
- `make INSIDE_DEV_CONTAINER=1 check-interfaces`: **passed**, including compiled
  ABI/layout checks, generated Rust/TypeScript consistency, ten negative cases,
  protocol/catalog/control/rollout/shard/inbox frontend regressions.
- `cargo test --offline --locked --manifest-path suifleet-edge/Cargo.toml`:
  **459 passed, 4 ignored** before the additional managed-shard signer test;
  that additional test separately **passed**.
- `cargo test --offline --locked --manifest-path suifleet-edge/Cargo.toml
  --features pkcs11 -- --test-threads=4`: **462 passed, 4 ignored**, including
  managed direct/group shard signing and mutability/registry negatives.
- `cargo test --offline --locked --manifest-path suifleet-indexer/Cargo.toml`:
  **48 passed**; production indexer build **passed**.
- `make INSIDE_DEV_CONTAINER=1 dapp-typecheck`: **passed**, including its full
  offline testnet/manifests/frontend/browser prerequisites.
- `make INSIDE_DEV_CONTAINER=1 test-ota`: **passed the complete suite**,
  including artifact failures/restarts, supervision/configuration, daemon updates,
  desired state, inboxes/handover, controls/telemetry/logs/console, rollout/cadence,
  budget/settings/recovery and all four fleet-data modes (normal, idle reads,
  compact history, compact history with quilt).
- `make INSIDE_DEV_CONTAINER=1 test-tracking`: **10 passed**; existing remote
  issue #257 and its seed acceptance now explicitly distinguish managed v3
  from sharded v4.
- `cd dapp && pnpm build`: **passed**, all application routes statically built.
- `python3 scripts/test_protocol_localnet.py --receipt-shards`: **passed**,
  compatible upgrade, old-package attacks, populated resumable migration,
  1,000 receipts and complete cleanup. See `sf127-localnet.json` (188 receipt
  transactions). Maximum single receipt gross **10,985,200 MIST**, below the
  **60,000,000 MIST** float; this is not a 120,000-device load benchmark.
- `python3 scripts/test_protocol_localnet.py --receipt-clients`: **passed**,
  early-package deletion attacks, actual daemon ordinary/catalog shard reports,
  schema-4 rollout health retry, production indexer and cleanup. See
  `sf127-clients-localnet.json`.
- `python3 scripts/test_protocol_localnet.py --managed-receipt-shards`:
  **passed**, pre-customer → current-main managed → sharded upgrade, preserved
  old type origins, populated managed status migration, previous managed-package
  attack effects during/after freeze, actual daemon report, independent frontend
  and production indexer reads, foreign-fleet/reassigned-device refusal, and
  complete bounded cleanup. See `sf127-managed-localnet.json` (42 successful
  digests and 16 expected failure effects).
- `python3 scripts/test_protocol_localnet.py --receipt-security`: **passed**
  the complete SF-001/SF-010 local-chain security gate, real daemon successful
  apply/durable report retry without updater replay, revocation and cleanup.
  Ledger allowance used **799,183,332 MIST / 850,000,000**, 41 transactions;
  net operator spend **709,149,376 MIST**. See
  `sf127-upgrade-security-localnet.json`.

PR279 harness corrections were consumed at pinned
`50df059cb127ff2c19dcda29c0f901c15e9b7b09`: 700M package reservation, 1.4B
package ceiling, 850M security ceiling and 150M wallet ceiling; sequence preflight
2.4B. The security harness reserves 20M for measured small ordinary fixture PTBs
instead of an unused 200M; explicit package/chunk/cleanup budgets, full-reservation
requirements, actual effects/unknown-outcome charges and non-replenishment remain.

Initial attempts exposed and repaired stale version/measurement expectations and
a stale manifest in the test's final read. An initial Rust run hit a temporary
loopback-port collision; the complete four-thread rerun passed. The older dev
image lacked the browser; the full frontend check passed using the pinned browser
image plus the checksum-verified transaction CLI. These failed attempts are not
reported as successful checks.

## Remaining gates

Exact pushed-head/current-base ordinary/security CI, automatic public-testnet
validation and fresh independent review remain required. They are not claimed
passed here and the worker does not poll hosted CI or merge. Physical acceptance
remains in SF-031/#32; actual mainnet, production providers and manual real-wallet
acceptance remain deferred under SF-087/#159 and SF-031/#32. Root-local report
serialization, operator-funded migration/cleanup, and unsharded indexer-created
deployments remain existing limitations.
