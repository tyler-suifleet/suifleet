# SF-141 implementation evidence

Codex running/configured model identifier: `gpt-6.1-sol`; no separate model build
identifier is exposed. Session `dispatch-e8db9eb8e02141c09f856f46c6d5ccd7`, branch
`work/suifleet/sf-141-9eaa59d4`. Commands below ran on 2026-10-24 America/Los_Angeles
(2026-10-25 UTC). No public-chain transaction or physical operation was performed.

## Toolchain and scope

Pinned dev image `suifleet-dev:sf035`, image digest
`sha256:31f727258ccd2eb7d00039c26bca6cf045d6d71e4d19fe51ac899f4f239e5b5b`:
Sui release compiler 1.81.1-e04cf9aa915c, transaction/local-validator CLI
1.82.1-987123842623, Rust 1.94.0, Node 22.22.2 and pnpm 11.0.8.
The checkout was mounted at `/workspace`; dedicated `sf141-move`, `sf141-cargo`
and `sf141-pnpm` volumes held disposable build caches. Local-chain runs used
`--network none`, no host ports, temporary keys, the pinned Move source cache
and read-only Git common-directory metadata. Browser session verification used
host Node 24.21.0 and real Chrome 153.0.8010.36, with controlled chain/wallet/Seal
fixtures; production typecheck/build used pinned Node 22.

Existing FleetData, DeviceData, Anchor and storage layouts/signatures remain
unchanged. New policy types require their own verified introduction origin.
The client opt-in and legacy callable-package boundaries are described in
[retention operations](../RETENTION_RENEWAL.md).

## Verification

| Command | Result |
|---|---|
| `sui move test --path contracts/fleet_storage` | Final 68/68 passed, including shared quota, protected-reserve isolation, fully paid admission, incompatible floor and attached-prepayment extension refusal. |
| `sui move test --path contracts/fleet_data --build-env testnet` | 40/40 passed: independent policy bounds, purchases, compact temporary index and expiry reclaim plus existing authority/cadence failures. |
| `python3 scripts/check_move_security.py` | Passed pinned compiler/strict linters, baseline checks, genuine negative authority/compiler cases; core 202/202, storage 68/68, data 40/40. |
| `cargo test --locked` / `cargo test --locked --features pkcs11` | 424 / 426 passed respectively; four existing ignored tests in each run, no new ignores. |
| `python3 scripts/test_fleet_data_ota.py --binary suifleet-edge/target/debug/suifleet --shim suifleet-edge/target/debug/examples/grpc_sim_shim --stream-retention --compact-history --quilt` | Passed real-daemon encrypted publication, lost committed-registration response, certification outage, long-outage retry, restart/deduplication, all three stream kinds, mutation refusal, preserved raw spool and independent SDK decrypt/signature checks. Simulator evidence, not public Walrus acceptance. |
| `node scripts/test_stream_retention.cjs` | Passed bounds, introduction origin, malformed/unreadable response refusal, owner transaction/extension builders and non-destructive stream selection. |
| `node scripts/test_stream_retention_panel.cjs` | Passed actual component owner edit, non-owner refusal, horizon validation and late response isolation after account change. |
| `node scripts/test_storage_reclaim.cjs` / `node scripts/test_storage_reclaim_panel.cjs` / `node scripts/test_fleet_data.cjs` | Passed planner, batching, expiry/current-record, signature/binding and preview/account/network isolation regressions. |
| `CHROME_EXECUTABLE=/opt/google/chrome/chrome node scripts/test_data_session_browser.cjs` | Passed real Data page session expiry, pending-read/plaintext/search isolation and fresh-session recovery. Retention controls have their separate component fixture. |
| `cd dapp && pnpm typecheck && pnpm build` | Passed pinned-toolchain typecheck and static export, including Data and Settings. |
| `make test-retention` | Passed existing Python worker and SDK/BCS/funding/expiry/scale compatibility regressions; not an executor deployment. |
| `python3 scripts/generate_interfaces.py --check` | Passed 12 compiled modules and generated interfaces; no generated ABI drift. |
| `SF141_RETENTION=1 SF129_RECEIPT=/workspace/docs/validation/sf141-localnet.json python3 scripts/test_reclaim_localnet.py` | Passed real existing-object upgrades, write/extension/expiry/reclaim lifecycle and rebate/effects checks. |
| `make check` | All application/test/build steps passed, including four real-daemon archive modes, final frontend typecheck/build and offline packaging/acceptance checks. Outer `make check` exited 2 solely at its last `git diff --check` because the container lacked the linked worktree Git common-directory mount. Re-ran that exact last check in a container with the correct read-only common-directory mount and on the host: both passed. This is a recovered environment setup failure, not an ignored software failure or an unqualified successful outer command. |
| `python3 -m unittest discover -s scripts/tests -p test_tracking.py`; `python3 scripts/bootstrap_tracking.py` | 10 tests passed and default offline dry run passed; no remote mutation/new issue. |
| `git diff --check` and documentation/catalog link consistency | Passed on host and correctly mounted container; changed documentation local links resolve. |

The broad suite started during implementation. Final changed areas were also
rechecked separately after the last source edits (68/40 Move tests, strict
security gate, final lifecycle, native and frontend checks above). No failure was
ignored: initial test-fixture certification, missing fixture ID/exports and browser
container-library problems were repaired or rerun using the stated host browser.
Raw private diagnostic logs are retained with the local worker evidence, not
published as scanner reports.

## Isolated-chain lifecycle

Receipt: [sf141-localnet.json](sf141-localnet.json). The source-reviewed development
prerequisite is exactly `76c186993e0a8ddf065360db5003028c3156cf04` (SF-129).
The fixture publishes that base, creates populated objects, upgrades core/storage/
data compatibly and verifies origin preservation and unchanged fleet, writer and deployment object bytes.
It then buys/certifies three- and five-epoch stream archives, writes a compact
root with a temporary expiry index, extends logs by one epoch from the owner
browser builder, and confirms the original epoch-3 expiry does not remove the
extended archive. At epochs 4 and 5, the corresponding stream is reclaimed and
its rebate matches the dry run's actual effects. The other stream stays live
until its own expiry. Early pruning is a real failed effect. A restart-style
rescan selects no reclaimed index; unanchored uploads remain held.

A separate empty protected-storage fixture at epoch 5 uses an already elapsed
owner floor at epoch 1 with zero executor reserves. Its fresh three-epoch purchase
succeeds through epoch 8, and no RenewalKey is created. Legacy pool admission
retains its old future-target check. This regression prevents a finite historical
floor from becoming a hidden renewal deadline for rolling fully paid uploads.

The receipt contains local chain `19c1aec0` and 78 successful transactions,
plus the explicitly failed early-prune effect. Its 2238859043 MIST successful-transaction
net cost includes fixture publications/upgrades and is **disposable local gas**,
not a public-wallet spend or a production cost estimate. Each successful
transaction records its digest, gas components and net MIST; failed effects include
their own gas evidence. The expected failed prune cost 1000770 MIST;
including that failure, total disposable-fixture net gas is 2239859813 MIST.

Final contract-source SHA256:

- `fleet_data.move`: `5eca3a0cc49a2ba0c77c3ed6ad5a14d140c6665e82dd187ff01c66fea5a02fa9`
- `fleet_storage.move`: `e7623b02bdd4562a00b4296b6999a9862431261899858490aa6db7ed58fc2d78`

## Remaining gates and rollout

SF-129 must merge before final SF-141 integration. The Project/ticket/PR handoff
retains that dependency; development before its merge is the explicit user
exception for this reviewed pinned source. No public epoch-563 prune/rebate or
SF-146 fixture reconciliation is established here. Hosted exact-head/current-base
CI/security, automatic public Testnet and fresh independent review remain pending
and mandatory; this worker does not watch or poll hosted CI.

Upgrade both packages, preserve all prior origins, explicitly migrate v1/v2
storage, record the new stream-policy introduction origin, configure periods,
fund quotas and stage daemon/signer opt-in. Old pending batches keep their API;
old clients/packages retain legacy pool behavior. These policies cover the
supported logs/diagnostics/crash chain archives, not an unrelated exporter or the
local IPC spool. Periods are 1–53 Walrus epochs, subject to the actual protocol
horizon. Longer lifetime needs an owner-approved extension before expiry. Expiry
removes unavailable items from the live view; reclaiming deposits still requires
an owner wallet transaction. No unattended server, automatic debit or guaranteed
renewal/cleanup is deployed. Unanchored ciphertext and unverified raw spools are
preserved, including when owner pruning leaves an offline device awaiting recovery.

Physical, actual Mainnet, production-provider/account and manual real-wallet
acceptance remains explicitly unrun in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).

## PR #307 feedback repair (2026-10-25)

Merged snapshot main `3c6625c33b238914a2b22d4b7c1a3dd1a69aff27` in
`ecd619b5`, preserving both data-usage and retention controls, both simulator
modes and both tracked acceptance entries. Fixed the Settings browser fixture
to supply controlled wallet hooks to the actual retention panel. The upstream
usage panel is isolated in the Seal-session browser fixture, with usage metering
verified separately. Production components are unchanged by the fixture repair.

Fresh merged-tree checks (not the historical runs above):

- `make test-contracts` passed all three packages: core 207, storage 68 and fleet-data 40.
- `cargo test --locked --offline --manifest-path suifleet-edge/Cargo.toml`: 437 passed, four existing ignored; `cargo build --locked --offline --manifest-path suifleet-edge/Cargo.toml --bins --examples` passed.
- `make test-fleet-settings` with real Chrome passed the complete settings, cost, budget and browser suite.
- `node scripts/test_stream_retention.cjs`, `test_stream_retention_panel.cjs`, `test_storage_reclaim.cjs`, `test_storage_reclaim_panel.cjs`, `test_fleet_data.cjs` and `test_data_metering.cjs` passed.
- `CHROME_EXECUTABLE=/opt/google/chrome/chrome node scripts/test_data_session_browser.cjs` passed session expiry, pending reads and recovery after updating the integrated-page fixture.
- `make dapp-typecheck dapp-build` passed with pinned Node in `suifleet-dev:sf035`, including its manifest/testnet offline prerequisites and static export.
- Full `make test-ota` passed, including its fresh daemon build, all update/recovery/configuration/security/reporting simulators and all five archive modes (standard, idle reads, compact, compact + Quilt, compact + Quilt + stream retention).
- `make test-retention`, 10 offline tracking tests and tracking publication dry run passed.
- `python3 scripts/generate_interfaces.py --check` passed all 12 modules.
- `cargo test --locked --offline --manifest-path suifleet-edge/Cargo.toml --features pkcs11`: 439 passed, four existing ignored.
- `python3 scripts/check_move_security.py` passed strict compiler/linter checks, all package tests and genuine negative authorization mutations.
- `SF141_RETENTION=1 SF129_RECEIPT=/tmp/sf141-repair-localnet.json python3 scripts/test_reclaim_localnet.py` passed existing-object upgrade, per-stream purchase, expiry and reclaim on a disposable network-none chain. No public gas was spent. The temporary receipt was not retained; the command success and lifecycle assertions are fresh evidence, while the committed original receipt above remains historical.

An initial Move attempt mounted its cache at the wrong home and failed to resolve
a dependency with networking disabled; the corrected `/home/dev/.move` mount
passed. The first merged Data browser run exposed the usage-widget fixture gap
and failed; its corrected fixture passes. No failed assertion was ignored.

Hosted exact-head/current-base CI, automatic Testnet and fresh independent review
remain coordinator gates after push. SF-129 must be merged before integration.
Deferred physical/Mainnet/provider/manual-wallet acceptance remains unrun in
#32/#159. No production deployment, physical operation or public-wallet mutation
was performed.

## PR #307 current-base integration repair (2026-10-25)

Integrated snapshot/current main `82591d81a130e8eb03f2e176590cf991347aed30`.
The independent review reported no blocking source defect, but the branch no
longer included the base. Resolved six conflicting files by preserving both
stream retention and SF-139 opaque support: additive Move APIs and tests,
pending-journal fields, signer fixtures, simulator state and shared/owned object
classification in the gRPC shim. No existing type layout or origin was changed.

Extended the stream signer regression to reject foreign storage, system, clock
and device-cap objects, and incorrect mutability for every shared authority
input. Added a second `make test-opaque` mode with stream retention enabled.
It verifies that every opaque/log purchase uses the stream API, while the first
mode verifies legacy registration remains unchanged. Both modes retain real IPC,
durable custody, certification-outage/restart, independent SDK decryption and
index-retirement assertions. These are offline fixtures, not provider acceptance.

Fresh checks used `suifleet-dev:sf035`, dedicated `sf141-move` and `sf141-cargo`
caches, no container network, and the host Chrome for browser checks:

- `make test-contracts`: core 207/207, storage 68/68, data 47/47 passed.
- `python3 scripts/check_move_security.py`: passed strict compiler/linter gates
  and genuine negative authority mutations.
- `cargo test --locked --offline --manifest-path suifleet-edge/Cargo.toml`:
  447 passed, four existing ignores. Locked offline bins/examples build passed.
- `cargo test --locked --offline --manifest-path suifleet-edge/Cargo.toml --features
  pkcs11`: 449 passed, four existing ignores. A fresh default-feature filtered
  `stream_purchase_signer_binds_all_authority_and_requires_opt_in` run passed
  (1 test) after extending the negative fixture coverage.
- `make dapp-typecheck dapp-build`: passed all prerequisites, typecheck and
  static production export. Stream retention/panel, reclaim/panel, fleet-data
  and data-metering Node fixtures passed.
- Host `CHROME_EXECUTABLE=/opt/google/chrome/chrome make INSIDE_DEV_CONTAINER=1
  test-fleet-settings` and `node scripts/test_data_session_browser.cjs`: passed
  real-browser Settings and Data session-expiry/isolation checks.
- `make test-retention`: passed 15 Python tests and the SDK compatibility fixtures.
- `python3 scripts/generate_interfaces.py --check`: passed all 12 modules.
- Offline tracking: 10 tests and publication dry run passed; no remote mutation.
- `SF141_RETENTION=1 SF129_RECEIPT=/tmp/pr307-localnet.json python3
  scripts/test_reclaim_localnet.py`: existing-object upgrade, per-stream purchase,
  expiry and reclaim passed on disposable network-none localnet. Its temporary
  receipt was not retained; the original committed receipt remains historical.
- Full `make test-ota` with `INSIDE_DEV_CONTAINER=1 CARGO_NET_OFFLINE=true`:
  passed the fresh bins/examples build and every updater, supervision,
  configuration, recovery, reporting, budget and archive simulator, including
  all five archive modes. A disconnected simulator request emitted a server
  BrokenPipe traceback during dependency tests; the dependency assertions and
  complete outer command passed (exit 0).
- `make test-opaque` as root in a disposable network-none container: Python SDK
  test and both real-daemon modes passed, including the new combined mode.

Initial Rust cache mounts used the wrong directory (missing registry, then
hiding the executable); corrected registry-only mount passed. The first interface
check omitted its Move cache and failed offline dependency resolution; rerunning
with the correct cache passed. These setup failures were not ignored assertions.

Review-note triage (limitations remain explicit, not claimed fixed): registration
kind is not bound to the later anchor kind; this does not cross the owning device's
WAL/upload quota boundary. The pre-existing 32-anchor UI window can omit older
longer-lived archives. The Data page still uses the existing testnet Walrus
configuration. Owner pruning of an expired compact root can require explicit
recovery of an offline device's pending journal, as documented above. The signer
negative-coverage note is addressed by the expanded regression in this repair.
These observations remain attached to SF-141's PR/issue for follow-through;
this repair does not silently promise malicious-device retention, unbounded
archive browsing, mainnet configuration or automatic destructive replay.

SF-129/#264 remains open and must merge before final SF-141 integration. Hosted
checks for the new pushed head and current base, automatic public Testnet and a
fresh independent review remain coordinator gates; this worker neither watches
nor polls hosted CI. Physical/Mainnet/production-provider/manual-wallet checks
remain explicitly unrun in #32/#159. No public wallet mutation, production
deployment, physical operation, new issue or other worker was started.


## PR307 current-base Data browser integration (2026-10-25)

Merged snapshot/current main `9b3799fe3838d6b4e94bd116f4ecbe181b8294c4`.
The shared Data page retains both FleetDataBrowser and StreamRetentionPanel.
The archive-session fixture isolates the newly imported generic browser, whose
own real Chromium harness remains exercised separately.

Fresh checks passed:
- Network-none `suifleet-dev:sf035`, `cd dapp && pnpm typecheck && pnpm build`.
- Host `make INSIDE_DEV_CONTAINER=1 test-manifest` (complete prerequisites).
- Node fixtures: test_stream_retention.cjs, test_stream_retention_panel.cjs,
  test_data_browser.cjs, test_storage_reclaim.cjs, test_storage_reclaim_panel.cjs.
- Host Chrome test_data_browser_browser.cjs and test_data_session_browser.cjs:
  real generic browsing/export/sharing and archive session-expiry checks passed.
- `git diff --check` passed.

Initial container `make dapp-typecheck dapp-build` passed test-testnet but
stopped in test-manifest because Chrome is installed only on the host. The
complete manifest prerequisites then passed on the host, and explicit pinned
container typecheck/build passed. The session harness initially timed out when
the newly imported browser attempted to use providers outside this fixture;
a scoped component fake restored its intended archive-session isolation, and
the full session assertions then passed. No failed assertion was ignored.

No contract/daemon code changed; their previous results are historical, not new
runs. Latest independent source review reports the opaque pruning fix resolved.
Its SF129 upgrade-order note remains relevant: do not publish SF129 pruning
without opaque-item cleanup. SF129 PR279 was OPEN on read; final integration
still requires its merge. Hosted checks/automatic testnet are pending and were
not watched. Fresh independent review remains required. Physical, mainnet,
production-provider and manual-wallet checks remain unrun in #32/#159.


## PR307 device-inbox base integration (2026-10-25)

Integrated snapshot/current main `f076a6334d75539ef3ba447484924cfa2896cb38`
in merge `4be054f033bba4f5c721f9c602e5babd63385c4d`. The only conflicts
were the reviewed ABI catalog and generated bindings: both SF129/SF141's
`deployment_manager::schema_version` and main's `scheduled_start_ms` are
preserved. Regenerated bindings match all 13 compiled modules. Existing source
review found no actionable defect; its dependency and pending-gate notes remain.

Fresh checks on the combined tree, using network-none `suifleet-dev:sf035`
with the existing dedicated caches (browser checks used host Chrome):

- `make test-contracts`: core 227/227, storage 68/68, data 49/49 passed.
- `python3 scripts/check_move_security.py`: strict compiler/linter and negative
  authority mutations passed, including inbox announcement/member negatives.
- `python3 scripts/generate_interfaces.py --check`: 13 modules passed.
- `cargo test --locked --offline --manifest-path suifleet-edge/Cargo.toml`:
  456 passed, zero failed, four existing ignores. Same command with
  `--features pkcs11`: 458 passed, zero failed, four existing ignores.
- `cd dapp && pnpm typecheck && pnpm build`: passed static production export.
- Node fixtures `test_stream_retention.cjs`, `test_stream_retention_panel.cjs`,
  `test_storage_reclaim.cjs`, `test_storage_reclaim_panel.cjs`, `test_inbox.cjs`
  and `test_fleet_ops.cjs`: all passed.
- Host `CHROME_EXECUTABLE=/opt/google/chrome/chrome node scripts/TEST.cjs`
  for `test_data_session_browser`, `test_data_browser_browser` and
  `test_fleet_settings_browser`: all real Chromium harnesses passed.
- `make test-retention`: 15 Python tests and SDK/BCS/expiry fixtures passed.
- `python3 -m unittest discover -s scripts/tests -p test_deployment_manifest.py`:
  19 passed.
- `SF141_RETENTION=1 SF129_RECEIPT=/tmp/pr307-current-localnet.json python3
  scripts/test_reclaim_localnet.py`: real existing-object upgrade, stream
  purchase, extension, expiry and reclaim passed on disposable isolated localnet.
  Temporary receipt stayed in the container; committed older receipt is historical.

Initial interface generation mounted the Move cache at the wrong home directory
and failed offline resolution; corrected `/home/dev/.move` passed. The first
localnet attempt lacked the linked-worktree Git metadata mount and failed before
the lifecycle; adding read-only common/worktree metadata mounts passed. Neither
setup failure was treated as success.

SF129 PR279 remains open and must merge before final integration, with its
merged source reconciled against this branch. Existing review notes about
opaque items awaiting explicit reclaim and the prior scope limitations remain
recorded, not silently relabeled fixed. No public expiry-window rebate evidence
is claimed. Hosted exact-head/current-base ordinary/security CI, automatic
testnet and fresh independent review remain coordinator gates after push.
Physical/mainnet/production-provider/manual-wallet acceptance stays deferred
and unrun in #32/#159. No public writes, deployment, hardware, issue creation,
merge or other worker launch occurred.

The complete `INSIDE_DEV_CONTAINER=1 CARGO_NET_OFFLINE=true make test-ota`
then passed (outer exit 0), including a fresh locked bins/examples build,
all updater/supervision/configuration/recovery/reporting/budget scenarios, the
new inbox simulator and all five archive modes, ending with compact + Quilt +
stream retention. A disconnected simulator client emitted a BrokenPipe traceback
during log testing; its assertions and the complete suite subsequently passed.

Main advanced during the run to `2169f7979c1c446b838f04a46e05e31a168697d6`
with only coordinator repair-ceiling code/docs/tracking changes. Integrated it
cleanly in `5a78468a`; no application code changed after the application checks.
`python3 -m unittest discover -s tools/pr-coordinator/tests -p
test_repair_budgets.py` passed all five relevant regressions. Staged, worktree
and current-base `git diff --check` passed. Both merge commits use Tyler Bush
for author and committer. Hosted checks for the resulting pushed revision are
pending and were not watched/polled; this is a feedback handoff, not approval.

## PR307 SF129 reclaim reconciliation (2026-10-25)

Integrated SF129 PR279 head `290d1b011c7699d36703d19dbd54f47a70dd1b42`
in `bc27e7b9`. Snapshot and freshly fetched current main
`2169f7979c1c446b838f04a46e05e31a168697d6` are ancestors. PR279 was
still OPEN on read; its merge remains required before final SF141 integration.

Resolved pruning onto SF129's contiguous `MAX_ANCHOR_ITEMS` loop, preserving
SF141 stream retention, compact AnchorKey/reuse marker and matching CompactHead
removal. Adopted SF129's verified item origins and complete/bounded/resumed item
counting. Each candidate reserves `3 + items` from the 256-removal budget for
its anchor, reuse marker, possible compact head and item indexes. Full 16-item
batches select 13 anchors; loose blobs fill only the remaining weighted budget.
The browser uses SF129's exact `readAnchorAt` lookup across pruned gaps.

Fresh verification (all commands exited zero):

- Network-none `suifleet-dev:sf035`, checkout `/workspace`, existing sf141 Move
  cache and read-only Git metadata: `make INSIDE_DEV_CONTAINER=1 test-contracts`
  passed core 227, storage 68 and data 50 tests.
- Same container: `python3 scripts/check_move_security.py` passed strict
  compiler/linters, compatibility baselines and genuine authority mutations;
  `python3 scripts/generate_interfaces.py --check` passed all 13 modules.
- Same container: `SF141_RETENTION=1 SF129_RECEIPT=/tmp/pr307-weighted-localnet.json
  python3 scripts/test_reclaim_localnet.py` passed real existing-object upgrade,
  per-stream purchase/extension/expiry/reclaim and opaque index disappearance.
  Temporary receipt stayed inside the disposable container; committed older
  receipts are historical. No public gas was spent.
- Network-none same pinned image: Node fixtures `test_storage_reclaim.cjs`,
  `test_storage_reclaim_panel.cjs`, `test_stream_retention.cjs`,
  `test_stream_retention_panel.cjs` and `test_data_browser.cjs` passed;
  `cd dapp && pnpm typecheck && pnpm build` passed static production export.
- Host `node scripts/test_storage_reclaim.cjs` passed again after the final
  assertion that 13 full batches leave room for exactly nine loose blobs.
- Host `CHROME_EXECUTABLE=/opt/google/chrome/chrome node
  scripts/test_data_browser_browser.cjs` passed real Chromium fixture acceptance.
- Host `python3 -m unittest discover -s scripts/tests -p test_testnet.py`:
  36 tests passed, covering inherited SF129 testnet allowance regressions.
- Staged/worktree `git diff --check` passed. Tyler author/committer identity
  verified for the reconciliation. Rust and full OTA results above are historical;
  this repair changes no daemon code and does not claim new Rust/OTA runs.

Hosted exact-head/current-base CI, automatic public-testnet checks and fresh
independent review remain pending coordinator gates after push; this worker did
not poll them. Deferred physical/mainnet/production-provider/manual-wallet checks
remain unrun in #32/#159. No production deployment, hardware operation, public
wallet mutation, issue creation, other worker or PR merge occurred.


## PR307 dependency-blocked base refresh (2026-10-25)

The latest independent review found no actionable source defect and verified the
previous weighted-reclaim repair. Its sole blocker remains unresolved: a fresh
`gh pr view 279 --json state,mergedAt,mergeCommit,headRefOid` returned OPEN,
no merge commit, at `290d1b011c7699d36703d19dbd54f47a70dd1b42`.
SF-129 must merge before SF-141 final integration; this worker cannot merge it.

Integrated snapshot and fetched main
`fdf3dd33c451f32e155a0bc768850087da8374ad` without conflicts. This adds upstream
pricing regressions and the legacy acceptance-deployment discovery repair;
SF-141 application sources are unchanged by the merge.

Fresh checks on the merged tree (all exit zero):

- `python3 -m unittest discover -s scripts/tests -p test_subscription_pricing.py`: 10 passed.
- `python3 -m unittest discover -s scripts/tests -p test_testnet_deployment_calls.py`: 3 passed.
- `python3 -m unittest discover -s scripts/tests -p test_testnet.py`: 36 passed (offline fixtures, no public transactions).
- `node scripts/test_storage_reclaim.cjs`, `node scripts/test_storage_reclaim_panel.cjs`,
  `node scripts/test_stream_retention.cjs`, `node scripts/test_stream_retention_panel.cjs`
  and `node scripts/test_data_browser.cjs`: all passed.
- `git diff --check` and `git diff --check origin/main`: passed.

No new Move/Rust/full-OTA/browser/build/live tests were run in this refresh;
earlier evidence above is historical. Hosted checks for the new pushed head
remain pending coordinator enforcement and were not polled. Fresh independent
review and dependency verification remain required. Physical/mainnet/provider/
manual-wallet acceptance is deferred and unrun in #32/#159. No public writes,
production deployment, hardware programming, issue creation or merge occurred.


## Recovered current-main and pinned SF-129 integration (2026-10-25)

Same SF-141 owner and branch resumed under the operator's explicit recovery.
Codex model: `gpt-6.1-sol`. Integrated current main
`b819cb2010ac4890e4d2d3bb5dad62b9df9b6a8d` and the explicitly pinned,
independently reviewed SF-129 head
`50df059cb127ff2c19dcda29c0f901c15e9b7b09`. Interface coordination was posted
in [PR 279](https://github.com/tyler-suifleet/suifleet/pull/279#issuecomment-6404451978)
and [PR 307](https://github.com/tyler-suifleet/suifleet/pull/307#issuecomment-6404452229).
SF-129 remains a strict **merge-only** prerequisite. Carrying its pinned source
here does not establish that it merged or that public expiry-window rebates ran.

The eleven main conflicts and subsequent pinned-head conflicts are reconciled.
The combined storage implementation preserves the managed-customer allowance
reservation and the finite, WAL-quota-bounded stream purchase. New additive
`register_managed_stream_blob` and `register_managed_blob_for_epochs` entry points
bind the customer/fleet and atomically charge the encoded-byte allowance. Legacy
entry points reject managed budgets. Rust selects the charged call and its signer
checks customer/fleet IDs and mutability, opt-in, all existing objects and bounds.
No existing public signature or layout was changed by this repair. The completed
implementation and localnet-helper commits are `33791c47`, `6c80079c`, `ea2afa7c`,
`fdf5af58` and `44bce4be`; the two parent reconciliations are `210a3f89` and
`80ad3b99`.

Retention controls use the selected fleet and freshly discovered administrator
credential; the existing managed transaction wrapper borrows and returns the
custodied cap. Archive plaintext, sessions and pending reads are invalidated on
fleet changes. Real Chrome regression now changes fleet during a pending read
and proves that its old plaintext cannot publish. The duplicate browser export
and duplicate interface JSON keys introduced by integration were removed.

The localnet harness saves the head storage tree once, upgrades it after core,
and inspects the live layout version before migration (the SF-141 base is already
v3). It preserves owner, exclusive-expiry, retirement and type-origin checks.
The reviewed transaction gas accounting in `scripts/live_package_upgrade.py`
remains unchanged. Privileged metadata reads retain main's 900-second budget;
the separate keyless probe retains this branch's reviewed 3700-second bound.

Fresh checks used `suifleet-dev:sf037` with network disabled, checkout mounted at
`/workspace`, the `sf141-move` Move cache and `sf141-cargo` mounted at
`/home/dev/.cargo/registry`. Browser checks used host Chrome at
`/usr/bin/google-chrome`. Commands and results:

- `INSIDE_DEV_CONTAINER=1 make test-contracts`: core 261/261 and storage 74/74.
  The newly added fleet-data positive fixture initially used an invalid synthetic
  blob ID; corrected it to Walrus `derive_blob_id`, then
  `sui move test --path contracts/fleet_data --build-env testnet`: 60/60 passed.
- `python3 scripts/check_move_security.py`: passed strict compiler/extra linters,
  core 261, storage 74, data 59 and genuine negative authority mutations. This
  run preceded the additional managed positive data fixture; that fixture's
  fresh result is the 60-test run above.
- `cargo test --locked --offline --manifest-path suifleet-edge/Cargo.toml`:
  459 passed, four existing ignored. This preceded the additional managed-stream
  signer test. `--features pkcs11`: 462 passed, four existing ignored, including
  that test. After making its module binding explicit for the ABI scanner, the
  default-feature filtered `managed_stream_signing_requires_charged_path_and_exact_authority`
  regression passed again. Locked offline bins/examples build passed.
- In pinned Node 22, `cd dapp && pnpm typecheck && pnpm build`: passed, including
  the final fleet-reset changes. `make test-manifest` with host Chrome passed,
  including real Chromium generic Data browsing and all existing fixture checks.
- `node scripts/test_stream_retention.cjs`, `test_stream_retention_panel.cjs`,
  `test_storage_reclaim.cjs`, `test_storage_reclaim_panel.cjs`: passed. The policy
  panel fixture now covers managed administrator, managed read-only and stale
  response refusal as well as legacy authority/bounds.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_data_session_browser.cjs`:
  passed expiry, late-read and selected-fleet reset/late-read assertions.
  `make INSIDE_DEV_CONTAINER=1 test-fleet-settings` with the same Chrome: passed.
- `python3 scripts/generate_interfaces.py --capture`, then `--check`: passed,
  14 core modules. `python3 scripts/check_contract_interfaces.py`: passed,
  990 references across all 16 first-party modules, zero drift. Duplicate optional
  origin keys were normalized; the retained values are unchanged.
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`: 133 tests
  OK, one existing skip. `test_live_common.py`: 40 OK; `test_live_fleet_reclaim.py`:
  12 OK. These are offline tests, not public transactions.
- `test_retention*.py`: 15 OK; `node scripts/test_retention.cjs`: passed SDK,
  cross-runtime BCS, policy, funding, batching and failure fixtures.
- Root network-none `python3 scripts/test_opaque_ota.py --binary
  suifleet-edge/target/debug/suifleet --shim
  suifleet-edge/target/debug/examples/grpc_sim_shim --stream-retention`: passed
  real IPC, durable outage/restart, exact purchase/ciphertext reuse, index retirement
  and independent pinned SDK decryption.
- Network-none `SF141_RETENTION=1 SF129_RECEIPT=/workspace/.sf141-localnet-receipt.json
  python3 scripts/test_reclaim_localnet.py`: passed existing-object upgrades,
  per-stream purchase, expiry, opaque item removal, compact history and reclaim.
  [Retained receipt](SF141_CURRENT_MAIN_LOCALNET.json) contains 78 local transactions
  and the genuinely failed pre-expiry prune. Its fixture base is the original
  SF-129 v3 base, not a claim to have replaced that base with a later publication.

- `CARGO_NET_OFFLINE=true INSIDE_DEV_CONTAINER=1 make test-ota`: full suite
  passed, including updater/platform, supervisor, configuration, daemon-update,
  desired-state/inbox/handover/control, telemetry/log/console, rollout, cadence,
  budget/settings/recovery and all five fleet archive modes (legacy, idle reads,
  compact, compact+Quilt, compact+Quilt+stream retention). A disconnected-client
  HTTP `BrokenPipeError` appeared in the daemon-update fixture; its scenario
  assertions passed and the complete outer Make suite reached the final PASS.

- Network-none `SF129_RECEIPT=/workspace/.sf141-reclaim-regression-receipt.json
  python3 scripts/test_reclaim_localnet.py` (default SF-129 mode): passed the
  existing-object core/storage/data upgrades, old-entry-point and owner/expiry
  refusals, archive/blob reclaim, desired-state retirement and deployment-record
  reclaim. [Retained regression receipt](SF141_CURRENT_MAIN_RECLAIM.json) records
  62 actual local transactions and measured rebates; these are not public expiry evidence.

Recovered setup/regression failures were not ignored: an initial registry mount
used the wrong Cargo path; the initial frontend aggregate requested a Chrome
binary absent from the container, so browser aggregation ran on the host and
production compilation ran with pinned container Node. The inherited controller
assertion expected the obsolete 150-second value; it now checks main's 900.
The first combined localnet run caught duplicate declarations; the next caught an
unneeded v3-to-v3 migration. Both are repaired and the final SF-141 run passed.
The ABI scanner initially lacked an explicit fleet-data module in the new signer
test; the test now declares and uses that module, and the scanner passes.

No public wallet mutation, hardware operation, mainnet write or production
credential/provider acceptance was performed. Physical, mainnet, final production
provider and manual real-wallet acceptance remain unrun in #32 and #159.
Hosted exact-head/current-base CI and automatic public-testnet checks remain for
the coordinator after push; no hosted CI was watched or polled. Fresh independent
review and SF-129's integration gate remain required; this is a repair handoff,
not approval or permission to merge.


### PR #307 retained-publication CI repair (2026-10-25)

Run [40247168337](https://github.com/tyler-suifleet/suifleet/actions/runs/40247168337)
resubmitted a retained 400,000,000-MIST base publication. New publications already
reserve 700,000,000 MIST; retained bytes cannot have their budget changed.
The pinned CLI returned `InsufficientGas` without JSON effects, leaving the
reservation and pending marker unknown. A read-only gRPC lookup of
`FTPAMqyDsrDMTj9VwxBSbJyezM4B8STk4H5uvYR56d6E` confirmed failed effects at
epoch 1264/checkpoint 399232091, sender and gas owner the dedicated CI wallet
`0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac`,
5,010,000 MIST net gas, and no created objects. No machine-session mutation ran.

Retained-byte submission now performs one exact-digest effects lookup after a
CLI error. Verified sender/gas-owner effects settle the ledger and allow the
existing setup path to clear a confirmed failed marker. Missing/unavailable or
foreign/mismatched effects preserve uncertainty and the full reservation.
There is no new submission retry, spend-ceiling increase, or marker deletion
based only on the CLI error. A subsequent run can reconcile the historical
failed transaction before building a fresh publication with the existing budget.

Fresh scoped checks: `python3 -m unittest discover -s scripts/tests -p
 'test_testnet*.py'`: 136 tests passed, one existing skip; `git diff --check`: passed.
The offline regression drives `run()` through retained-byte CLI failure and
independent failed-effects lookup, checking persisted marker removal, no adopted
package, failed digest evidence and allowance accounting. Negative cases retain
unknown reservations. Current base `b819cb2010ac4890e4d2d3bb5dad62b9df9b6a8d`
is already an ancestor; no merge commit is necessary. Application/Move/Rust suites
were not rerun for this harness-only delta. Hosted CI remains pending after push;
fresh independent review and the SF-129 merge dependency remain required.
Physical/mainnet/provider/manual-wallet acceptance remains deferred and unrun in
[#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159).

### PR #307 predecessor submission integration (2026-10-25)

Integrated the current SF-129 PR #279 head
`7b04f38eba94dd0ed977164d3b4a0e29c6d1b507`, including its signed-byte
execution path and full deployment/cleanup gas reservations. Current main and
the review snapshot base are both
`b819cb2010ac4890e4d2d3bb5dad62b9df9b6a8d`; main was fetched and merged
(already contained). SF-129 remains OPEN and a strict merge prerequisite.
This source pin replaces earlier pins for continued dependent development;
it does not establish SF-129 merge or public expiry-window rebate acceptance.

Removed the duplicate `submit_retained` recovery layer. All retained submissions
now use `exact_execution` (sign exact persisted bytes, then execute-signed-tx),
so CLI nonce regeneration cannot change their digest. The single effects recovery
path derives the lookup digest from signed bytes, checks returned digest and
verified sender/gas owner, and drops `reconciled` for an execution submitted by
this run. Known failed effects clear only the failed pending operation, charge
actual net gas, and permit a fresh bounded publication. Missing/unavailable,
foreign or mismatched facts preserve the pending bytes and full reservation.
Recovery does not submit another transaction. The inherited bounded gRPC
indexing retry remains in `executed_facts`.

Adapted the old exception-based regression to the predecessor's known-failure
flow: the durable fresh intent remains after a failed retained publication.
Added full offline run-flow tests for successful recovery/this-run attribution
and absent/unavailable/foreign sender/foreign payer/wrong-digest negatives.

Fresh verification on the integrated repair:
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`:
  138 tests, OK (one existing skip), 3.708 seconds.
- `git diff --check` and `git diff --cached --check`: passed.
- No Move/Rust/frontend/OTA/localnet suites rerun for this Python-harness-only
  delta; earlier receipts remain historical evidence.
- No public network writes, funding, deployment or hardware operations ran.

Hosted exact-head/current-base ordinary/security CI, automatic public-Testnet
acceptance and fresh independent review remain pending after push and are not
watched or waived. The coordinator alone merges, after verifying SF-129 merged.
Physical/mainnet/production-provider/manual-wallet acceptance stays deferred and
unrun in #32/#159. This repair handoff is not approval.

## Post-submission decoder feedback repair (2026-10-25)

Moved digest decoding ahead of exact signing/submission in the live upgrade
client. Effects recovery uses that precomputed digest, so a subsequent node
failure cannot be misclassified as a pre-submission refusal. Unknown outcomes
retain their reservation and pending operation for reconciliation.

Added an offline full-run regression for retained replay and fresh
`checkpoint_setup` publication. The decoder refuses calls after the simulated
submission; a lost CLI response and missing effects must keep the exact pending
bytes and full budget (400M retained / 700M fresh), with status `unknown`.

Fresh checks for this Python-only feedback delta:
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`:
  139 tests, OK (one existing skip), 3.795 seconds.
- `python3 -m unittest discover -s scripts/tests -p 'test_live*.py'`:
  53 tests, OK, 0.030 seconds.
- `git merge origin/main`: already up to date at b819cb2010ac4890e4d2d3bb5dad62b9df9b6a8d.
- No Move/Rust/frontend/OTA/localnet rerun for this scoped harness repair.
- No public-network mutations, hardware or deployments performed.

PR #279 / SF-129 remains OPEN at 7b04f38eba94dd0ed977164d3b4a0e29c6d1b507;
its verified merge is still required before SF-141 integration. Hosted CI and
automatic testnet on the pushed repair remain pending, unpolled; fresh independent
review is required. Deferred external #32/#159 evidence remains unrun. This
feedback repair does not approve or authorize merging the PR.

### Funding repair diagnosis — run 40253439621

The exact failed run's public `package-upgrade.json` artifact proves chain
`4c78adac`, protocol 139, a successful single faucet invocation (return code 0),
and no submitted transactions. Initial gas pools totaled 1,815,945,830 MIST;
the request credited 1,000,000,000 MIST, leaving 2,815,945,830 MIST. The required
3,000,000,000 MIST includes the unchanged 2.6 SUI selected-suite ceilings and
the 0.4 SUI unresolved historical quarantine hold. This is insufficient credit,
not a failed faucet or a newly uncertain submission. The old marker and hold
must remain until independently reconciled. No additional faucet request was
made by this worker, and no encrypted state was decrypted or modified.

The harness now records public before/after balance delta, without CLI output,
so subsequent funding repairs can distinguish successful partial funding.
An offline regression reproduces the exact balances and verifies one request,
zero mutations, zero allowance consumed and unchanged ceiling despite credit.
`python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`: 140 tests
OK, one existing skip. `test_live*.py`: 53 tests OK. `git diff --check`: clean.
Product source is unchanged; application suites were not rerun for this delta.

Funding recovery itself remains unverified: the next protected execution must
re-read balance and apply the existing single-request rule if needed. Absent
intervening spending, one additional ordinary run's 1 SUI credit would cover
this recorded shortfall; this is arithmetic, not a claimed live result. No
retry loop, higher allowance, removed reservation or fabricated success was
introduced. Hosted CI was not watched or polled. Independent review, current
CI/testnet and verified SF-129 merge remain gates. Physical/mainnet/provider/
manual-wallet checks remain unrun in #32/#159. The coordinator owns next dispatch.

## 2026-10-25 current-main reconciliation after independent review

Reconciled main `7d6607d451d1469c32a5a9debaf0f9826e29fcfe` into the owner
branch. Main's shared `PACKAGE_GAS`, 1.5-SUI upgrade-security ceiling,
`execute_exact` signing and digest-bound result/other-execution quarantine paths
supersede this branch's earlier parallel harness implementation. Kept the public
faucet balance diagnostics and their regression; kept the historical package/full
reservation regressions, updating the selected-suite total to main's 3.25 SUI.
The obsolete `exact_execution` helper/test and serialized-tx adapter were removed
in favor of main's tested signer/submitter paths. Product source is unchanged by
this reconciliation.

Fresh host checks: `python3 -m unittest discover -s scripts/tests -p
 'test_testnet*.py'`: 141 tests OK, one skip; `test_live*.py`: 53 OK;
`test_release*.py`: 27 OK. An initial merge-resolution run exposed a stale test
calling the superseded helper; the final runs above passed after reconciling that
test and its adapter with main. No Move/Rust/frontend/OTA/localnet/browser suites
rerun for this harness/base reconciliation; earlier evidence is historical.

SF-129 PR #279 is still OPEN, with no merge commit, so verified SF-129 merge
remains required before SF-141 integration. No public expiry-window rebate,
physical/mainnet/provider/manual-wallet result is claimed (#32/#159 retain the
external acceptance). No shared-wallet mutation, faucet, deployment or hardware
operation ran. Hosted checks for the pushed reconciliation remain pending and
unpolled; the coordinator must enforce exact-head/current-base CI/testnet and
fresh independent review. This handoff is blocked on the genuine merge
prerequisite, not approval.

## 2026-10-26 feedback/base reconciliation

Integrated snapshot/current main `79cfd1cb7645aaa294dd327cbee017acc95c8c75`
without conflicts. The incoming changes cover subscription pricing and tracking;
this integration changes no Move, daemon or frontend product source.
Fresh offline host checks: `python3 -m unittest discover -s scripts/tests -p
 'test_testnet*.py'` (141 OK, one skip), `test_live*.py` (53 OK),
`test_release*.py` (27 OK), and `test_subscription_pricing.py` (21 OK).
`git diff --check origin/main HEAD` passed. No application/localnet/browser
suite was rerun for this base-only integration; prior evidence remains historical.

Read-only GitHub metadata still reports SF-129 PR #279 OPEN at
`457dd49f6423b90ae2d5f6db6d70fc7753ea4ca0`, with no merge commit.
The review reported no new code defects; this genuine merge prerequisite remains
blocking. The coordinator must integrate verified merged SF-129 before final
SF-141 integration, then obtain exact-head/current-base CI/testnet and fresh
independent review. Hosted checks remain pending and were not polled. No live
mutation or hardware operation ran. Public expiry-window rebate and deferred
physical/mainnet/provider/manual-wallet acceptance (#32/#159) remain unclaimed.

## 2026-10-26 second feedback/base reconciliation

The independent review of `310c1fc5` reported no actionable source defects.
Integrated current main `bffb2c9010a0a215ffa2da02ac585bf7403aca01`
without conflicts; this is also the snapshot base. Incoming changes
include testnet reconciliation, cost collection and coordinator test maintenance.
No Move, daemon or frontend product source changed in this integration.

Fresh offline commands `python3 -m unittest discover -s scripts/tests -p PATTERN`
passed for `test_testnet*.py` (143 tests, one skip), `test_live*.py` (53),
`test_release*.py` (27), `test_subscription_pricing.py` (21),
`test_fleet_cost_collector.py` (21), and `test_observe_fleet_costs.py` (5).
`python3 -m unittest discover -s tools/pr-coordinator/tests -p
 'test_quota_recovery.py'` passed 55 tests. `git diff --check origin/main HEAD`
passed. No application, localnet or browser suite was rerun for this integration.

Read-only metadata reports SF-129 PR #279 OPEN at `698b5649`, with no merge
commit. This genuine merge dependency remains blocking; do not substitute its
older source pin for a verified merge. The coordinator must reconcile its eventual
merged form, enforce exact-head/current-base CI and automatic testnet, and obtain
fresh independent review. Hosted CI is pending and was not polled. No live writes
or hardware operations ran. Public expiry-window rebate and deferred physical,
mainnet, provider and manual-wallet evidence remain unclaimed (#32/#159).

## 2026-10-26 receipt-sharding base integration

The review of `2668b49a` reported no actionable code defects. Merged snapshot
and fetched current main `8ab0aefa96223d974775f08d0a6314ea27b411c1`
without conflicts, incorporating receipt sharding and its client/runtime APIs.
The merge commit is `d4f090f487635f27714732a0ebbf0b51351d62b7`.

Fresh verification used the pinned `suifleet-dev:sf035` image, network disabled,
with disposable SF141 caches and read-only Git metadata for the local chain:

- `make INSIDE_DEV_CONTAINER=1 test-contracts check-interfaces`: passed all
  three Move packages, compiled ABI comparison (1077 references, no drift),
  generated interfaces (14 modules) and protocol/catalog/control/rollout/
  receipt-shard/browser-cleanup/inbox regressions.
- `CARGO_NET_OFFLINE=true make INSIDE_DEV_CONTAINER=1 edge-test`: passed
  default 462 tests and PKCS11 464 tests, four existing ignored tests per variant.
- `cd dapp && pnpm typecheck && pnpm build`: passed.
- `SF141_RETENTION=1 SF129_RECEIPT=/tmp/pr307-current-localnet.json python3
  scripts/test_reclaim_localnet.py`: passed real existing-object upgrade and
  per-stream purchase/expiry/reclaim. Temporary chain keys remained disposable.
- Host `node scripts/test_stream_retention.cjs`,
  `test_stream_retention_panel.cjs`, `test_storage_reclaim.cjs` and
  `test_storage_reclaim_panel.cjs`: passed.
- Host `python3 -m unittest discover -s scripts/tests -p PATTERN`:
  `test_testnet*.py` 145 OK (one skip), `test_live*.py` 66 OK,
  `test_release*.py` 27 OK. These are offline mocked-network tests.

The initial host Rust invocation could not select a default toolchain; the
successful Rust checks above used the pinned image instead. No test failure
was ignored or replaced. No public-wallet writes, faucet, deployment or physical
operation ran.

Read-only PR metadata still reports SF-129 / PR #279 OPEN at
`3b0bcb5b663dac61f81497d8e6d9f2de6d185551`, no merge commit. Its verified
merge and reconciliation remain required before SF-141 integration. Hosted
CI and automatic testnet for the pushed head remain pending, unpolled; fresh
independent review remains required. The coordinator alone merges. Public
expiry-window rebate and deferred physical/mainnet/provider/manual-wallet
acceptance remain unrun in #32/#159. This handoff is not approval.

- `CARGO_NET_OFFLINE=true make INSIDE_DEV_CONTAINER=1 test-ota`: the complete
  rebuilt-debug-daemon suite passed, including supervisor/configuration/daemon
  handoff, desired state/inbox/handover, controls/telemetry/logs/console,
  rollout/cadence/budget/settings/recovery and all five fleet-data variants,
  ending with compact-history + quilt + stream-retention. This is simulator
  evidence, not physical boot/recovery or public-provider acceptance.

## 2026-10-26 funding-only CI recovery — run 40310094389

The failed exact-head run at `5904a4710ddf632915dcd162b7223586079519d1`
(base `8ab0aefa96223d974775f08d0a6314ea27b411c1`) submitted no
transactions and consumed zero allowance. Its public artifact records one
successful 1-SUI faucet credit, leaving 2,826,511,937 MIST below the
3,650,000,000-MIST preflight requirement: selected suite ceilings total
3,250,000,000 MIST plus the preserved 400,000,000-MIST historical hold.
No code defect or new uncertain transaction was identified.

A separate bounded funding-only recovery acquired `network:suifleet-testnet`,
coordinated in SF-010/#11 and asserted the lock before the faucet operation.
The checksum-pinned transaction CLI (SHA-256
`de1d7effd53831af2863749befc35ec6df565b1fdfa8d8b2f5c866be23fa0d6b`)
verified chain `4c78adac` and read the dedicated CI address's gas pools.
A disposable public-only client configuration with an empty keystore was used;
no CI signing credential was accessed. The balance was still exactly the failed
run's final balance, so one faucet request was made, with no retry.

Faucet transaction `5mYvY4Cb2yUrN6jdTqjas6yRVWUy7pw6JRUHQSDPRZnk`
was independently read through `sui client tx-block --json`: effects success,
checkpoint 399398346, and balance change +1,000,000,000 MIST to
`0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac`.
The faucet paid its own 1,000,000-MIST computation fee; no dedicated-wallet
transaction or gas spend occurred. A fresh `gas --json` read shows
3,744,773,212 MIST in address balance plus 81,738,725 MIST in a coin, totaling
3,826,511,937 MIST, 176,511,937 above the existing requirement. The global
lock was promptly released. No allowance, quarantine marker, encrypted fixture,
spend ceiling, test or retry policy was changed. No fixture transaction was
replayed. This balance snapshot does not guarantee funds remain unused later.

Fresh local checks:
- `git merge origin/main`: already up to date at `8ab0aefa`.
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`: 145
  tests OK, one existing skip (7.170 seconds).
- `python3 -m unittest discover -s scripts/tests -p 'test_live*.py'`: 66 tests
  OK (0.039 seconds).
- Product code is unchanged; Move/Rust/frontend/OTA/localnet suites were not
  rerun for this funding-only recovery.

SF-129 PR #279 is still OPEN with no merge commit; verified integration of
its merged form remains required before SF-141 merges. The coordinator must
obtain actual exact-head/current-base CI and automatic testnet results and fresh
independent review. Hosted checks were not watched or polled. No approval is
claimed. Physical/mainnet/provider/manual-wallet acceptance remains unrun in
#32/#159. No deployment, hardware programming or production credentials were used.

## 2026-10-26 reviewed SF-129 source reconciliation

Reconciled exact independently reviewed SF-129 head
`e5e5c452753b33e2a7bc538cc20c12a0510b223a` on this SF-141 branch.
Freshly fetched main remains `8ab0aefa96223d974775f08d0a6314ea27b411c1`
and was already an ancestor. SF-129 remains a merge-only dependency; preparing
its source does not claim PR #279 merged or satisfy its public expiry acceptance.
The supplied latest free planner evidence is epoch 562, no eligible anchors,
expiry 563. No new public reads/writes or expiry/rebate result is claimed here.

The merge keeps the newer funding/unknown-outcome accounting and all three
funding regressions in `test_testnet.py` (the remaining content conflict).
Previously reviewed resolutions preserve strict full-budget reservations and
faucet balance-change evidence. The funding controller, toolchain and package
runner have no net changes from the prior SF-141 head. Retention contracts,
opaque-item cleanup, managed-customer authority, daemon source and generated
bindings likewise have no net changes. Incoming fixes select receipt-directory
schema for retirement, refuse live receipt roots, recover bounded partially
pruned history, and reconcile throttled submissions using the same signed bytes
and a verified digest while retaining unknown reservations.

Fresh scoped checks on the combined tree:

- Pinned `suifleet-dev:sf035`, network none, `make INSIDE_DEV_CONTAINER=1
  test-contracts check-interfaces`: core 290, storage 74, data 60 passed;
  1077 ABI references, zero drift, 14 generated modules and interface negative
  tests passed, including receipt-shard/managed cleanup compatibility fixtures.
- Same image/network, `cd dapp && pnpm typecheck && pnpm build`: passed.
- Same image/network, `SF141_RETENTION=1 SF129_RECEIPT=/tmp/pr307-reconcile.json
  python3 scripts/test_reclaim_localnet.py`: real existing-object upgrade,
  purchase, expiry, opaque-item cleanup and reclaim passed. Receipt and keys
  remained disposable; no new public-chain evidence was committed.
- Host `python3 -m unittest discover -s scripts/tests -p 'test_live*.py'`:
  73 passed. `test_testnet*.py`: 145 passed with one existing skip.
  `test_retention*.py`: 15 passed. These tests use offline fixtures.
- Host Node fixtures: `test_storage_reclaim.cjs`,
  `test_storage_reclaim_panel.cjs`, `test_incremental_history.cjs`,
  `test_receipt_shards.cjs`, `test_stream_retention.cjs` and
  `test_stream_retention_panel.cjs`: passed.

Rust/OTA and real-browser suites were not rerun: their code and retained
production UI are unchanged in this delta; earlier results above are historical.
Hosted exact-head/current-base CI, automatic public-testnet and fresh independent
review remain mandatory and pending at handoff, without polling. SF-129 verified
merge remains required before final integration. Physical/mainnet/production
provider/manual-wallet checks remain unrun in #32/#159. No deployment,
hardware operation, production credentials or SF-129 owner-worktree edits ran.

- `python3 scripts/check_move_security.py` in the same pinned network-none
  image: exit 0; strict compiler/linter, public-signature compatibility and
  genuine negative authority mutations passed, with no FAIL diagnostics.
- Integration merge `5b4ca9a5dce6be838dabc944f0e87f3f8cda2743` has both
  the prior SF-141 head and exact SF-129 pin as parents; ancestor checks for
  that pin and fetched main passed. Whitespace checks passed.

## 2026-10-26 current-base readiness reconciliation

Merged snapshot/current main `6ae9d2cfb32291c69806dc901c243dd4c5b0fa43`
without conflicts. The incoming delta is only SF-156 acceptance-readiness tooling,
its tests, operational documentation and tracker records (7 files); retention,
Move, daemon, frontend and testnet runner sources are unchanged from `df112c61`.
The independent review found no actionable defect and no open threads.

Fresh host checks, all exit 0:
- `python3 -m unittest discover -s tools/pr-coordinator/tests -p 'test_acceptance_watch.py'`: 11 passed.
- `python3 -m unittest discover -s scripts/tests -p 'test_live*.py'`: 73 passed.
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`: 145 tests OK, one existing skip.
- `python3 -m unittest discover -s scripts/tests -p 'test_retention*.py'`: 15 passed.
- `git diff --check` and `git diff --check origin/main HEAD`: passed.

These are offline fixture tests; their printed transaction/faucet diagnostics are
simulations, not public execution evidence. No readiness service was installed or
started. Product builds, Move/Rust/OTA/browser/localnet tests were not rerun for
this tooling/documentation-only integration; prior runs above remain historical.
SF-129 PR #279 was checked read-only and remains OPEN at `e5e5c452`, mergedAt
null. Its exact reviewed source remains included, but verified merge is still a
final integration prerequisite. Public expiry/rebate evidence is not claimed.
Hosted checks for the pushed head are pending and will not be polled by this
worker; fresh independent review and actual exact-head/current-base CI/testnet
remain required. Physical/mainnet/provider/manual-wallet acceptance stays unrun
in #32/#159. No public mutations, deployment, hardware or other workers ran.

## 2026-10-26 console-base feedback reconciliation

Integrated snapshot/fetched main `633bbb5f88d21acb3281fba20ad2dcef7d147b4f`
in merge `1b11e282`. Resolved four conflicts: kept the incoming page header and
plain-language messages/balance disclosure, retained stream retention and scoped
archive reclaim controls, and retained archive-session fixture exports/import
isolation. Existing fleet-selection/session invalidation remains intact.

Fresh checks on the combined tree, all passed:

- Network-none `suifleet-dev:sf035`: `cd dapp && pnpm typecheck && pnpm build`.
- Host `node scripts/test_stream_retention.cjs`,
  `test_stream_retention_panel.cjs`, `test_storage_reclaim.cjs`,
  `test_storage_reclaim_panel.cjs`, and `test_data_browser.cjs`.
- Host Chrome (`CHROME_EXECUTABLE=/opt/google/chrome/chrome`):
  `test_data_session_browser.cjs`, `test_fleet_settings_browser.cjs`,
  `test_data_browser_browser.cjs`, and `test_dapp_presentation_browser.cjs`.
  Presentation covers 14 exported routes at desktop/mobile widths. Its initial
  run overlapped the build and read the prior export (navigation assertion
  failed); rerunning after the successful build passed.
- `python3 -m unittest discover -s scripts/tests -p 'test_retention*.py'`:
  15 passed; `test_tracking.py`: 10 passed.
- Working-tree and staged `git diff --check`: passed.

Move/Rust/OTA/localnet are unchanged by this reconciliation and were not rerun;
earlier full-suite evidence above remains historical. No public-chain writes,
service installation, hardware operations or production credentials were used.
Read-only PR #279 query still reports OPEN, mergedAt null, head `bcaa99c5`:
SF-129 verified merge remains a genuine integration dependency, not repaired by
merging main here. The independent review reports no actionable source defect.
Hosted checks for the pushed head remain pending/unpolled; the coordinator must
obtain exact-head/current-base CI/testnet and fresh independent review. Physical,
mainnet, provider and manual-wallet acceptance stays unrun in #32/#159, as does
public SF-129 expiry/rebate evidence. Only the orchestrator may merge SF-129 and
then reconcile its merged main into SF-141 before final integration.
