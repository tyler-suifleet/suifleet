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
