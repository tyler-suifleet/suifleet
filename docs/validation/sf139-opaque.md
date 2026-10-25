# SF-139 implementation evidence

Codex, actual configured/running model **gpt-6.1-sol**; no finer runtime build ID
is exposed. Existing session `dispatch-e7229f89cfcc45858a7568fb1e41535d`, branch
`work/suifleet/sf-139-50c724f5`, assigned worktree. The existing claim was asserted
and heartbeated throughout. No new ticket, worker, server product, hardware or
mainnet operation. Commits use Tyler Bush as both author and committer.

The clean branch fast-forwarded to main `c7548c4325a1a600bece25c8485af35faadd5361`,
which merged SF-121/#252, including pinned prerequisite `ec7cb67`. Both branches
were preserved. Contract/producer/scoped reader ownership was recorded in existing
[#284](https://github.com/tyler-suifleet/suifleet/issues/284#issuecomment-6389272518)
and [#285](https://github.com/tyler-suifleet/suifleet/issues/285#issuecomment-6389272749)
before overlapping API work. The concrete API is [OPAQUE_DATA.md](../OPAQUE_DATA.md).

## Implemented and exercised

- UID/stream-authorized `data_put` and Python SDK/CLI preserve arbitrary raw bytes,
  explicit chunk geometry, hashes and metadata. Partial backpressure retains the
  upload ID/failed chunk for resumed submission. Durable custody responses explicitly
  say `storage_acknowledged:false`.
- A locked, bounded, atomic queue retains custody during outages, overflow,
  corruption and uncertain submissions. No drop-oldest or retry-age exhaustion.
  Operator-selected lossless durable streams enter this same queue; only the last
  verified chunk advances their durable export checkpoint.
- Existing `fleet_archive::tick` / `Pending`, schema4 QuiltItem descriptors and codec
  own the single register/write/certify/anchor lifecycle. One-chunk Quilts are
  supported. Up to16 patches and8 KiB of combined index arguments share one anchor.
  Urgent log diagnostics keep priority; routine log/data fairness survives restart.
- Each patch has independent signed plaintext, scoped Seal identity and exact
  ciphertext/content digests. Immutable per-item metadata is indexed atomically
  with the certified blob anchor. Queue retirement requires exact independent
  anchor and item-index readback. Existing object layouts and legacy APIs survive.
- The reader verifies chain/fleet/writer/current identity, live storage/epoch,
  actual per-type origins, bounded Quilt geometry, ciphertext, Seal scope/AAD,
  personal signature, metadata and raw content; rechecks after decryption. A reader
  need not decrypt another stream or the fleet-wide manifest. Shared-batch transport
  currently downloads the bounded parent; it does not claim single-patch bandwidth.

## Local verification

Host Rust uses `CARGO_BUILD_JOBS=2`,
`CARGO_TARGET_DIR=/home/tyler/.local/state/suifleet/sf139-build` and Rust1.94.0.

- `cargo +1.94.0 test --locked --manifest-path suifleet-edge/Cargo.toml --features pkcs11`:
  **432 passed, 4 existing ignored, zero failed**. Includes intake authority,
  chunk/hash/corruption/queue capacity, restart, canonical signing and transaction
  intent bounds. Earlier default-feature run:427 passed/4 ignored, before the
  final added signer/ACL/optional-metadata tests; this is not relabeled as the final feature run.
- `cargo +1.94.0 build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples`:
  passed, actual debug daemon/shim/fixture signer produced.
- `/tmp/sf121-tools/sui move test --path contracts`: **190/190 passed**;
  `--path contracts/fleet_storage`: **61/61 passed**;
  `--path contracts/fleet_data --build-env testnet`: **29/29 passed**.
  Pinned compiler1.81.1; no new lint suppression.
- Disposable network-none `suifleet-dev:sf037`:
  `python3 scripts/test_protocol_localnet.py --fleet-data-upgrade`: passed.
  [Real local-chain effects/origins](sf139-localnet-upgrade.json) preserve existing
  fleet/writer bytes, exercise later type origins and new APIs, reject revoked
  devices through old/new packages, and test old/new fleet-wide approvals against
  120-byte scoped identities. Owner/stream grant, cross-device/stream denial and
  revocation probes pass. These are real chain simulations, not mocked policy success.
- Disposable root network-none container:
  `python3 scripts/test_opaque_ota.py --binary /build/debug/suifleet --shim /build/debug/examples/grpc_sim_shim`:
  passed with actual rebuilt native daemon, Unix IPC and Python SDK. Mixed raw
  binary/UTF-8 data, multiple chunks, large per-item metadata/index batching,
  durable stream flush, log fairness, prepaid/certification outage, restart,
  immutable ciphertext/purchase reuse and verified-index-only retirement pass.
  `verify_opaque_native.cjs` independently decrypts native ciphertext with the
  pinned real Seal SDK and verifies signatures, exact metadata, raw hashes,
  missing chunks, unverified/modified buffer or index substitution, verified
  assembly provenance, and pre-key device/stream/epoch/ciphertext tamper refusal.
  The gRPC/Walrus transport here is simulated and does **not** verify a quorum
  certificate. Local VM/chain policy/upgrade and native crypto evidence are
  separate from the actual public Walrus roundtrip below; no local Walrus cluster
  or production provider deployment is claimed.
- All **26** post-build `make test-ota` commands passed across the initial run,
  corrected rollout rerun and seven remaining commands, with checked exit codes.
  The first rollout run passed behavior but hit a pre-existing teardown race:
  TemporaryDirectory removed state while daemons still wrote it. The contained
  harness fix stops/reaps workers before directory cleanup; the scoped rerun passed.
  Earlier root/UID setup errors are not counted as passes.
- `python3 -m unittest discover -s scripts/tests -p test_opaque_sdk.py`: **1/1 passed**,
  partial full/backpressure retry preserves exact file bytes, chunk positions and hashes.
- Node22.22.2 TypeScript `node_modules/typescript/bin/tsc --noEmit` and production
  `node_modules/next/dist/bin/next build` in `dapp/`: passed; static export completed.
- `node scripts/test_archive_quilt.cjs`, `test_fleet_bulk.cjs`,
  `test_compact_archives.cjs`: passed. Actual pinned SDK two-patch geometry
  (manifest plus one item), legacy signed archives and compact history remain supported.
- `PATH=/tmp/sf121-tools:$PATH python3 scripts/check_contract_interfaces.py`:
  **692 client references /14 compiled modules, zero drift**.
- Offline tracking `python3 -m unittest discover -s scripts/tests -p test_tracking.py`:
  **10/10 passed**; `python3 scripts/bootstrap_tracking.py`: offline plan passed,
  preserving existing published SF139/SF140 IDs. `git diff --check`: passed.

Failures found during implementation were repaired, not ignored: initial AAD used
an unnormalized configured device address; one-item support required updating the
old manifest-count regression; the new fairness assertion initially mistook log
Quilts for opaque Quilts. Final checks use canonical IDs and identify opaque patches
by their scoped item descriptors. Early local-chain cache UID and frontend cwd
setup failures were corrected before the successful commands above.

## Public testnet, costs and limits

[Public effects and independent readback](sf139-testnet.json) record chain
`4c78adac`, the explicit dedicated agent wallet, verified reused immutable core/
storage dependencies, the new source package and cap, one certified Quilt and two
independently indexed items. Package bytecode was checked before signing;
cached source bytecode/cap ownership were reverified on final read-only replay.
Machine mutations held `network:suifleet-testnet`, coordinated in #11/#63;
the lease was released promptly. No faucet, allowance reset or device funding.

Actual binary5 bytes and UTF-8 text29 bytes were signed through the bounded native
synthetic-fixture bridge, independently Seal encrypted, prepared/written by the
existing codec, certified and anchored with one item-index call. Actual owner
reads recovered exact bytes; a binary-only scoped grant denied text, and a fresh
session after revocation denied both while owner reads still succeeded. Legacy
support membership gave no scoped approval. The native daemon itself was exercised
in the simulator above; this is not called a public daemon rollout.

Eight effects were independently reread, including signed net refunds. Charged
allowance: **111,475,710 /500,000,000 MIST**. Actual signed net gas:
**109,953,201 MIST**; the grant revocation's −1,522,509 MIST rebate does not replenish
the allowance. Package publication alone cost73,914,400 MIST and is cached, never
republished for the same source.

The **three** storage lifecycle calls (register/certify/anchor) cost
**24,307,894 MIST** net for this small fixture; no per-item transaction. The
445,556-byte Quilt contains5,383 ciphertext bytes for34 raw payload bytes and
metadata/signatures. It consumes **66,034,000 encoded bytes** and363,951 FROST
write fees from the existing prepaid finite pool. Pool purchase/retention cost is
not included in that write fee or the Sui transaction subtotal.

The two DataItem values are **239 and223 BCS bytes** (462 combined). The dynamic
field UID32/key16 adds96 more bytes, before object/type/system overhead. Anchor
effects charge17,419,200 MIST storage,1,000,000 computation and return3,989,601 rebate:
**14,429,599 MIST net**. This includes writer/anchor/marker mutations and both index
entries; it is not a measured marginal deposit per item. Longer names/metadata
and more chunks increase permanent index storage linearly. No pruning/rebate API,
fleet-scale gas bound, production invoice or affordability claim is supplied.
The test fixture remains indexed for review; the package/history have no deletion
API. Its finite storage epoch/capacity is not indefinite availability.

## Remaining gates

Pushed-head/current-base ordinary CI, automatic testnet status and fresh independent
source/security review remain coordinator gates; pending CI is never approval and
is not watched. Physical/Pi, actual mainnet and final production-provider/account/
manual IdP/prover/Slush/extension-wallet acceptance remain explicitly **unrun** in
[#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159). No production credentials,
paid services, physical programming, mainnet writes or merge authorization is inferred.

## PR302 independent-review repair (2026-10-24)

Codex gpt-6.1-sol repaired the feedback on head `85f9041785df` in the existing
SF139 claim/worktree. `git fetch origin main` and `git merge --no-edit origin/main`
confirmed snapshot/current base `c7548c4325a1a600bece25c8485af35faadd5361` was
already included; no artificial merge commit was needed.

- Durable stream collection now requests up to16 messages /384 KiB per stream,
  rather than one message. A whole stream message reserves and commits all of its
  chunks together; insufficient space leaves its source checkpoint unchanged and
  permits older custody to publish. A queued final chunk prevents recopying
  earlier retired chunks while later chunks await publication. FIFO verified
  acknowledgement and the16-item/8 KiB archive limits remain enforced.
- Immutable `*.item` files replace repeated payload rewrites in `queue.json`.
  Intake loads only the compact sequence/retry/length index; a duplicate reads
  only its matching chunk. Archive reads still validate every loaded payload.
  No full queue clone occurs. Index replacement and directory scans remain
  proportional to pending item count; this is not constant-time intake. One-time
  legacy migration preserves sequences/descriptors/source checkpoints and commits
  the new index before deleting legacy custody. Interrupted unreferenced writes
  and retired files are reclaimed under the same lock. Old queue.json-only readers
  cannot consume the migrated format: drain custody before downgrading.
- Regression tests cover65 independent chunk intakes with an untouched/corrupted
  earlier payload (new intake succeeds without reading/rewriting it, publication
  fails closed), a compact index below16 KiB at that size, a new chunk file
  below48 KiB, legacy migration, orphan cleanup, atomic stream capacity rejection,
  retries/restarts and4,096-item backpressure.
- The actual daemon/SDK test now appends six stream messages, verifies all six
  immutable indexes with multiple messages sharing a batch, and checks the final
  source checkpoint. The independent SDK verifier groups by stream **and upload**
  so distinct messages are verified separately. Certification outage/restart,
  ciphertext/purchase reuse, binary/text assembly, signed metadata, scope tamper
  and log fairness checks remain exercised.
- Finding3 takes the reviewer's explicitly accepted documentation option(b),
  preserving the ticket's public content SHA-256 format. The first section of
  OPAQUE_DATA.md now prominently discloses exact public sizes and unsalted plaintext
  hashes, offline candidate guessing and the lack of low-entropy confidentiality.
  This repair does **not** introduce salted commitments or claim to eliminate that
  exposure. Application-side encryption requires separately held keys.
- The nonblocking source-reconfiguration note is documented with the fail-closed
  recovery requirement: restore the original stream definition/state and finish
  verified retirement; never edit custody journals to force acknowledgement.

Verification on the repaired source:

- `CARGO_BUILD_JOBS=2 CARGO_TARGET_DIR=/home/tyler/.local/state/suifleet/sf139-build cargo +1.94.0 test --locked --manifest-path suifleet-edge/Cargo.toml --features pkcs11`:
  **435 passed, zero failed,4 pre-existing ignored**.
- Same environment, `cargo +1.94.0 build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples`:
  passed; rebuilt actual daemon, gRPC shim and signing examples.
- Disposable root/network-none `suifleet-dev:sf037`, mounting that build:
  `python3 scripts/test_opaque_ota.py --binary /build/debug/suifleet --shim /build/debug/examples/grpc_sim_shim`:
  passed, including six-message batching and independent pinned SDK verification.
- `/tmp/sf121-tools/sui move test --path contracts/fleet_data --build-env testnet`:
  **29 passed, zero failed**. Contract/ABI/layouts were not changed by this repair;
  the earlier real upgrade/testnet evidence above is historical, not a fresh run.
- Node22.22.2 in the same disposable image, from `dapp/`:
  `node node_modules/typescript/bin/tsc --noEmit && node node_modules/next/dist/bin/next build`:
  passed; static production export completed. Reader source is unchanged.
- `python3 -m unittest discover -s scripts/tests -p test_opaque_sdk.py`:
  **1 passed**. `python3 -m py_compile scripts/test_opaque_ota.py`,
  `node --check scripts/verify_opaque_native.cjs`, and `git diff --check`: passed.

- All **26 post-build Makefile `test-ota` commands passed** against the rebuilt
  default-feature daemon, across the initial16 successful commands, corrected
  CloudWatch rerun and the remaining9-command run. Commands were generated with
  `make -n INSIDE_DEV_CONTAINER=1 test-ota` (excluding the already completed locked
  build) and executed sequentially with `bash -ex` in disposable network-none
  `suifleet-dev:sf037` containers. All three final fleet-data variants (legacy,
  compact-history, compact-history+Quilt) passed. No command failure was ignored:
  the initial root run stopped at CloudWatch because that adapter correctly
  rejects root. Diagnostic stderr confirmed that refusal. A first non-root UID1000
  setup could not read this private UID1001 checkout; the actual full CloudWatch
  test passed as `--user 1001:1001`, including daemon/file ingestion, outage,
  lost response, SIGKILL, rotation, sibling-file denial and credential negatives.
  The remaining9-command run exited0. No source change was needed for setup.
- Both changed documents' relative Markdown links resolve; `git diff --check`
  passed after recording the final results.

Hosted checks and automatic testnet on the pushed head remain pending coordinator
checks, with no polling or fabricated approval. No machine-session testnet write,
new publication, hardware, production-provider credential or mainnet operation
was performed during repair. Deferred external acceptance remains in#32/#159.

After the initial repair push, main advanced to
`233f4605e6d6e7c7c9f5c646e27d5528f001b948` (reviewed SF147 dashboard refresh).
The owner branch merged that new base before final handoff. Its changes are
limited to dashboard/coordinator documentation and tracking; `git diff --cached
--exit-code eb5b1b87bc150a177917ea417fb314c6b3b19efb -- suifleet-edge contracts
dapp scripts sdk interfaces` confirmed the tested application/contract/SDK source
was unchanged. Additional merged-base checks passed:
`python3 -m unittest discover -s tools/pr-coordinator/tests -p test_dashboard.py`
(**45 tests**) and `python3 -m unittest discover -s scripts/tests -p test_tracking.py`
(**10 tests**). The merge preserves the upstream implementation; no coordinator
runtime/database or service was changed by this worker.


## Current-base testnet failure follow-through (2026-10-24)

The next feedback snapshot retained head `a5d526a`, but main had advanced to
`de238f73a6b12b9fccca8e257be6fc1a3b265714`. Independent source re-review
reported no remaining actionable code/security findings. The current base was
merged into the owner branch; the merge changes coordinator dispatch and tracking
only, with no changes to the reviewed application, contracts, SDK or harnesses.

The failed automatic run [40140891303](https://github.com/tyler-suifleet/suifleet/actions/runs/40140891303)
was inspected once, without watching hosted CI. Package/deployment validation and
independent receipt effects succeeded before the upgrade-security suite exhausted
the existing three bounded public Walrus publisher attempts with HTTP 500.
The log reports package allowance use 83,878,186 MIST and upgrade-security
allowance use 53,225,847 MIST (four transactions in the latter suite); these are
historical run reports, not a new successful testnet result. No attempt was made
to reset spending, increase retries or claim an unavailable certification passed.
A new head needs its own successful current-base CI/testnet and independent review.

This repair performs no new public network mutation, deployment, hardware or
mainnet operation. The detailed PR reply records new local integration checks.
Earlier full application, simulator, local-chain and feature-specific testnet
results above remain historical evidence for unchanged software, not tests rerun
by this base-integration repair. Physical/real-wallet/provider/mainnet acceptance
remains unrun in #32 and #159.

New checks after base integration (all passed):

- `python3 -m unittest discover -s tools/pr-coordinator/tests -v`: 384 tests,
  including bounded testnet repair ownership/dispatch and real-process recovery.
- `CARGO_BUILD_JOBS=2 CARGO_TARGET_DIR=/home/tyler/.local/state/suifleet/sf139-build cargo +1.94.0 test --locked --manifest-path suifleet-edge/Cargo.toml --features pkcs11 opaque::`:
  8 passed, 431 filtered out; full-queue custody rejection included.
- `python3 -m unittest discover -s scripts/tests -p 'test_live_common.py' -v`:
  35 offline helper tests passed; negative cleanup diagnostics are expected fixtures.
- `python3 -m unittest discover -s scripts/tests -p '*tracking*' -v`:
  10 passed; `python3 scripts/bootstrap_tracking.py`: offline dry run passed,
  118 prepared issues with no remote or local publication mutation.
- `python3 -m unittest discover -s scripts/tests -p 'test_opaque_sdk.py' -v`:
  1 passed. `git diff --check` and all three relative validation-note links passed.

No new full simulator, frontend build, Move/localnet or live roundtrip was run in
this integration repair, since those sources did not change. Hosted CI and
public-testnet checks for the pushed head remain pending and were not watched.

## Repeated publisher outage diagnosis (2026-10-24)

Repair worker inspected failed automatic run
[40148688146](https://github.com/tyler-suifleet/suifleet/actions/runs/40148688146)
once. Despite the status's `source` classification, its traceback is an external
HTTP 500 from the configured public Walrus publisher in `Walrus.store`, reached
while preparing the upgrade-security legacy deployment. The existing three
bounded attempts were exhausted. This is the same failure location as the prior
run, not a failed opaque-data assertion or a package compatibility failure.

The run reports chain `4c78adac`, dedicated CI operator `0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac`,
reused verified package fixtures, successful deployment/receipt checks and
independent gRPC effects checks before the outage. Historical log reports:
package-upgrade allowance 63,853,114 MIST (three transactions), upgrade-security
allowance 53,225,847 MIST (four transactions), with the latter reporting 0.0113
SUI net spent. These are log reports, not a fresh independent spend reconciliation
or a successful complete automatic run. Existing fixture/recovery/spend records
must remain intact.

Offline reproduction added in `scripts/tests/test_live_common.py` confirms that
three publisher HTTP 500 responses propagate failure rather than acknowledge
storage or return a blob ID. `python3 -m unittest discover -s scripts/tests -p
test_live_common.py -v`: **36 passed**. No production code, retry limits, spend
limits, provider configuration or acceptance gates changed. `git merge --no-edit
origin/main` reports already up to date at snapshot/current base
`de238f73a6b12b9fccca8e257be6fc1a3b265714`.

The provider failure is **unresolved**. The next step is recovery of the configured
public publisher followed by a genuine bounded automatic testnet run; any endpoint
replacement needs verified public-testnet storage/certification compatibility and
source review, not an arbitrary fallback or local stand-in. This worker made no
new wallet mutation, blindly replayed transaction or hosted-CI rerun and did not
watch hosted CI. Fresh exact-head/base checks and independent review remain
required. Physical, mainnet, production-provider and manual real-wallet acceptance
remains unrun in #32/#159. This diagnosis and regression do not constitute a fix
of the external service or merge approval.

### Publisher recovery integration (2026-10-24)

Integrated main `3b7f364` into the SF-139 owner branch. Main includes the
independently reviewed SF-146 fallback `432fa73` as `c6a3c47` and wallet
publisher selection as `aea8038` (the implementation also supplied in
`68942fa`). No duplicate cherry-pick or change to the SF-146 worktree was
needed. Shared production harness files match main; opaque-data application,
SDK and scoped-decryption interfaces are unchanged. The merge resolves the
SF-139 persistent-500 regression to exhaust all publishers and bounded passes
without returning a blob ID, instead of expecting single-publisher retries.

Fresh scoped checks after integration:

- `python3 -m unittest discover -s scripts/tests -p test_live_common.py`:
  38 passed.
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`:
  80 tests OK, one skipped.
- `test_live_fleet_data.py` and `test_opaque_sdk.py` through the same unittest
  discovery command: one test each, passed.
- `python3 -m unittest discover -s scripts/tests -p 'test_live*.py'`:
  39 tests OK.
- `python3 -m py_compile scripts/live_common.py scripts/live_wallet_gate.py`
  and `git diff --check origin/main HEAD`: passed.
- Fresh `Walrus('testnet').working_publisher()` probe: Mysten returned
  HTTP 500, "could not find WAL coins with sufficient balance"; the pinned
  `https://publisher.walrus-01.tududes.com` stored the fresh probe, whose bytes
  were independently read back through Mysten's aggregator. Publishers paid;
  no wallet, Sui transaction, funding, shared lease or run allowance was used.
  This is publisher-selection evidence, not full wallet/feature acceptance.

Existing strict spend/uncertainty accounting, certification, anchor/effects
verification and read-back remain enforced. No new full chain, Rust, Move,
OTA or frontend build run is claimed for this harness-only resolution.
Exact-head/current-base hosted CI, automatic testnet (including the shared
wallet fixture) and fresh independent review remain required after push.
Physical acceptance (#32) and final provider/manual-wallet/mainnet acceptance
(#159) remain deferred and unrun. No merge approval is claimed.

### Current-base recovery integration (2026-10-25)

Integrated snapshot/current main `56215ecb0a6e4ee50b7a8eb58c185cc8f5eb1233`
(SF-146 reconciliation and per-suite funding) after the independent review of
`1dab86f` reported no actionable source/security findings. The merge was clean;
`git diff --exit-code 1dab86f HEAD -- suifleet-edge contracts dapp sdk interfaces`
passed, confirming no application, contract, SDK or interface change.

Fresh offline checks after integration:

- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`:
  116 tests OK, 1 skipped.
- `python3 -m unittest discover -s scripts/tests -p 'test_live*.py'`:
  39 tests OK (publisher errors and synthetic transaction logs are test fixtures,
  not live transactions or effects evidence).
- `python3 -m unittest discover -s scripts/tests -p test_opaque_sdk.py`:
  1 test OK; corresponding `test_tracking.py`: 10 tests OK.
- `python3 -m py_compile scripts/ci_testnet_wallet.py scripts/live_package_upgrade.py scripts/sui_toolchain.py scripts/testnet_ci.py scripts/testnet_reconcile.py`:
  passed. `node --check` for `scripts/sui_tx_inspect.cjs` and
  `scripts/testnet_reconcile.cjs`: passed. `git diff --check`: passed.

Rust, Move, OTA, frontend and public testnet were not rerun by this repair;
previous evidence remains historical. This resolves base drift only. Exact-head
hosted CI, automatic testnet including shared-wallet fixtures, and fresh
independent evidence review remain required and pending coordinator collection.
No hosted CI polling, live-wallet mutation, production deployment or hardware
operation occurred. Deferred physical and final provider/manual-wallet/mainnet
acceptance remains unrun in #32 and #159.

### Batched-polling base integration (2026-10-25)

Integrated snapshot/current main
`e543b9d1dd7b156d3300dfbf4914dc4eb2615789`, including SF-122 polling,
shared funding and bounded publisher fixes. The latest independent review of
`5b15244` reported no actionable source/security findings; its old-head checks
are historical, not approval for this integration.

The archive merge retains opaque batch selection, completed-batch fairness and
verified index retirement alongside main's schedule and pre-write funding checks.
Opaque exports bypass the empty-log shortcut: IPC/stream intake does not populate
the log spool and must remain discoverable after idle periods. Publisher tests
retain both upstream fallback/integrity negatives and the persistent-all-publisher
500 refusal to acknowledge storage. No contract, SDK or interface migration was
introduced by the merge.

Fresh checks:

- Rust 1.94.0, `CARGO_BUILD_JOBS=2` and private
  `CARGO_TARGET_DIR=/home/tyler/.local/state/suifleet/sf139-build`:
  `cargo +1.94.0 test --locked --manifest-path suifleet-edge/Cargo.toml --features pkcs11`:
  **441 passed, 4 pre-existing ignored**; corresponding
  `cargo +1.94.0 build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples`:
  passed.
- Every **28 post-build command** from the current `make test-ota` recipe,
  followed by the opaque round trip below, passed sequentially in disposable
  network-isolated containers (**29 commands total**). This includes idle poll
  reads, firmware/supervisor/daemon handoff, configuration, deployment control,
  telemetry/logs/console, rollout, cadence, budget/fleet settings, recovery,
  legacy/compact/Quilt archives and independent browser/SDK verification.
- `python3 -m unittest discover -s scripts/tests -p 'test_live*.py'`:
  **41 OK**; `-p 'test_testnet*.py'`: **116 OK, 1 skipped**;
  `-p test_opaque_sdk.py`: **1 OK**; `-p test_tracking.py`: **10 OK**.
  Synthetic transaction/cleanup diagnostics are mocked failure-path evidence.
- Network-isolated `suifleet-dev:sf037` container, fresh daemon/examples:
  `python3 scripts/test_opaque_ota.py --binary suifleet-edge/target/debug/suifleet --shim suifleet-edge/target/debug/examples/grpc_sim_shim`:
  passed, including independent native signature/Seal SDK decryption, binary/text
  assembly, scope/epoch tamper rejection, multi-message stream batches, outage and
  restart recovery, exact ciphertext/purchase reuse and verified index retirement.
- In the same pinned container, `node node_modules/typescript/bin/tsc --noEmit`
  and `node node_modules/next/dist/bin/next build` from `dapp/`: passed.
  The initial pnpm wrapper refused a noninteractive dependency purge; direct Node
  entry points avoid dependency mutation. Existing root-owned `.next` output
  required running the disposable build container as root.
- `python3 -m py_compile scripts/tests/test_live_common.py scripts/test_opaque_ota.py`,
  `node --check scripts/verify_opaque_native.cjs` and `git diff --check`: passed.

No fresh Move/local-chain or public-testnet run is claimed by this repair;
contracts are unchanged. Hosted exact-head/current-base ordinary/security CI,
automatic public-testnet validation and fresh independent review remain required
after push. Hosted CI was not polled. Deferred physical acceptance (#32) and final
provider/manual-wallet/mainnet acceptance (#159) remain unrun. No production
service deployment, wallet mutation, physical programming or mainnet operation.

### Fleet-data metering base integration (2026-10-25)

Merged snapshot/current main `58fce5b1d73ffc3d1b75c378a8e668124416e3a9`
(SF-142) cleanly. The latest independent review found no actionable source or
security defect; pending hosted checks are not approval. `git diff --exit-code
ec8f035 HEAD -- suifleet-edge contracts sdk interfaces` passed: daemon, contracts,
SDK and interfaces are unchanged by this integration.

Fresh network-none checks in pinned `suifleet-dev:sf037` containers:

- `node scripts/test_data_metering.cjs` and `node scripts/test_archive_quilt.cjs`:
  passed, including typed metering failures and actual pinned Quilt geometry.
- From `dapp/`, `node node_modules/typescript/bin/tsc --noEmit` and
  `node node_modules/next/dist/bin/next build`: passed; static pages exported.
- `node scripts/test_fleet_settings_browser.cjs` in `suifleet-dev:sf010-chrome`
  with `CHROME_EXECUTABLE=/opt/chrome-headless-shell/chrome-headless-shell`:
  passed, actual Chromium settings/pricing/deposit/transaction/failure cases.
- `python3 scripts/test_opaque_ota.py --binary /build/debug/suifleet --shim
  /build/debug/examples/grpc_sim_shim`: passed with the prior verified private
  debug build mounted read-only at `/build`; daemon sources are unchanged.
  Real IPC/chunk intake, stream batches, outage/restart custody, immutable
  ciphertext/purchase reuse, verified indexes and independent pinned-SDK
  signature/Seal binary/text retrieval all passed.
- Offline `python3 -m unittest discover -s scripts/tests -p 'test_live*.py'`:
  41 OK; `-p 'test_testnet*.py'`: 116 OK, 1 skipped;
  `-p test_opaque_sdk.py`: 1 OK; `-p test_tracking.py`: 10 OK.
- `git diff --check`: passed.

Initial browser attempt lacked Chromium in sf037; the browser-equipped pinned
image passed. Initial non-root Next build could not unlink existing root-owned
output; disposable root container passed. Initial opaque attempt required root
for its IPC admin fixture; the root container passed. No test failure was ignored.

The review's nonblocking opt-in opaque idle-read limitation is retained: empty
opaque-enabled queues bypass the log-only idle shortcut and can repeat chain
reads each poll after the interval. Default-device idle savings remain intact.
A future cheap durable queue/stream wake check must preserve post-idle discovery,
stream checkpoints and restart recovery; no such optimization is claimed here.
Record this within existing SF-139/#284 rather than create an unauthorized issue.

No fresh Rust compilation, Move/local-chain or public-testnet execution is claimed
for this frontend-base integration. Prior evidence remains historical. Hosted
exact-head/current-base CI, automatic testnet and fresh independent review remain
required after push; no hosted CI was watched or polled. Physical #32 and final
provider/manual-wallet/mainnet #159 acceptance remain deferred and unrun.
