# SF-127 implementation evidence — 2026-10-24

Owner: Codex, actual configured model **gpt-6.1-sol**, reserved session
`dispatch-badf2eb8e0a047d48632fda079139097`, branch
`work/suifleet/sf-127-55a48d6e`. The live operator clarification in
[issue #257](https://github.com/tyler-suifleet/suifleet/issues/257) supersedes the
original borrowed-global-map requirement. No dependencies or new issues added.

## Behavior and security boundaries

See [the protocol](../SHARDED_RECEIPTS.md). The implementation preserves existing
public signatures/layouts; introduces sixteen fixed-size shared roots with
individual receipt/index fields; rejects legacy v3 receipt reads; and copies v2
receipts behind a resumable barrier. Reports borrow the deployment immutably.
Cleanup is resumable and must retire every root before parent deletion.

Two source findings were repaired within this ticket:

- Early packages have no schema check in deletion. Moving group targets away
  from their unconditional old removal key prevents orphaning shared roots.
  Catalog bindings similarly move away from old catalog deletion paths.
- Older v2 packages can write receipts without rollout admission. Migration
  preserves their ordinary status, but only a fresh admitted v3 report supplies
  rollout settlement provenance. Existing coordinator outcomes remain intact.

Future receipt authority generations reject old migration/cleanup operations.
The daemon refuses unsupported receipt authority before applying schema-4 work.

## Exact checks and results

No hosted CI was awaited. Public-testnet and independent review remain required
coordinator gates after push, not successful acceptance recorded here.

| Command | Result |
|---|---|
| `sui move test --path contracts` | 204 passed, 0 failed. Migration/cursor/idempotence, copied receipt provenance, legacy getter rejection, revoked device, group targeting, wrong partition, bounded pages, future authority and full cleanup covered. |
| `python3 scripts/check_move_security.py` | Passed pinned compiler/strict Sui linters, unchanged unsuppressed compatibility baseline, all three package suites and deliberate authority-bypass negatives. No new suppression/baseline allowance. |
| `python3 scripts/check_move_proofs.py --tools /tmp/sf050-tools` | 63 positive phase obligations and all 25 exact negative controls passed. This is the existing conditional authority proof scope, not a formal proof of the new sharding protocol. |
| `make check-interfaces` (`INSIDE_DEV_CONTAINER=1`) | Passed compiled ABI/layout drift, generated clients, independent wire vectors and frontend protocol/catalog/control/rollout/shard tests. |
| `cargo +1.94.0 test --locked --manifest-path suifleet-edge/Cargo.toml` | 414 passed, 0 failed, 4 existing Docker/OCI integration tests ignored. |
| Same Rust command with `--features pkcs11` | 416 passed, 0 failed, the same 4 existing OCI tests ignored. Signer tests include both v3 calls, read-only deployment, wrong cap/registry/status and extra-command refusals. |
| `cargo +1.94.0 test --locked --manifest-path suifleet-indexer/Cargo.toml` | 47 passed, 0 failed. |
| `cargo +1.94.0 build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples` | Passed; real daemon and gRPC simulator used below. |
| `cargo +1.94.0 build --locked --manifest-path suifleet-indexer/Cargo.toml` | Passed; real indexer used below. |
| `pnpm typecheck && pnpm build` in `dapp/` | Passed; production static export completed. Locked dependencies, Node 22.22.2 / pnpm 11.0.8. |
| `python3 scripts/test_protocol_localnet.py` | Passed real frontend/generated-client lifecycle: register/activate/group/cap reissue, v3 receipts and bounded deletion, desired state, controls, rollout failure settlement and native multisig handover. |
| `python3 scripts/test_protocol_localnet.py --receipt-shards` | Passed real base→head compatible upgrade, preserved 70 v2 receipts over two bounded copy steps, old report/delete and wrong-shard/partial-finish refusals, 1,000-target reporting and complete root/field cleanup. |
| `python3 scripts/test_protocol_localnet.py --receipt-clients` | Passed actual pre-identity package upgrade and rejection of its unguarded delete; actual daemon effects-checked v3 Failed report after missing recipe; actual production indexer independently materialized the receipt; bounded cleanup. |
| `python3 scripts/test_protocol_localnet.py --receipt-security` | Passed the full normal SF-001/SF-010 security runner on a disposable local chain: base state, compatible upgrade/origins, old/superseded/pre-activation/impersonation/revoked reports, populated v3 migration, real daemon durable Applied retry with new provenance and no updater replay, cleanup and key-funding sweep. 41 effects-checked transactions; unchanged 600,000,000-MIST ceiling. |
| `make test-testnet test-tracking` (`INSIDE_DEV_CONTAINER=1`) | Offline only: 80 testnet/toolchain tests, 37 live-common regressions, authenticated cache negatives and 10 tracking tests passed. No remote mutation. |
| `python3 -m unittest discover -s scripts/tests -p test_deployment_manifest.py` | 17 passed. |
| `python3 -m unittest discover -s scripts/tests -p test_publish.py` | 36 passed; expected refusal diagnostics are negative tests. |
| `git diff --check` | Passed. |

The compiler/local chains ran in `suifleet-dev:sf010-chrome`, with the pinned
`sui 1.81.1-e04cf9aa915c`, read-only shared Move dependency cache and no network
interface except loopback. Local tests using Git history mounted the common Git
directory read-only at its original absolute path. The full security runner and
offline installed-toolchain test additionally mounted the checksum-pinned
`/tmp/sf137-network/sui` at `/opt/sui-network/sui` read-only: Sui 1.82.1,
SHA-256 `de1d7effd53831af2863749befc35ec6df565b1fdfa8d8b2f5c866be23fa0d6b`.
That was only for transactions on these disposable local chains, not public writes.

## OTA suite execution context

Every one of the 23 Python recipe commands under `make test-ota` passed. The host
invocation first failed in supervisor process inspection because the host user's
`/proc/1/cwd` was unreadable. In a disposable network-none container, the first
15 commands (through `scripts/test_logs.py`) passed as root. The CloudWatch
adapter then correctly refused to run as root; this was not bypassed. The final
eight commands, beginning with `scripts/test_cloudwatch_logs.py`, passed as the
normal dev UID in a fresh network-none container with its own PID namespace.
These are equivalent full recipe coverage in appropriate execution contexts,
not a claim that the failed host/root `make test-ota` invocations returned zero.

The executed command list is exactly:

```bash
python3 scripts/test_ota.py
python3 scripts/test_ota.py --platform-installer
python3 scripts/test_catalog_ota.py
python3 scripts/test_dependency_ota.py
python3 scripts/test_ota.py --firmware-helper
python3 scripts/test_firmware_restart.py
python3 scripts/test_supervisor.py
python3 scripts/test_configuration.py
python3 scripts/test_greengrass_runtime.py
python3 scripts/test_daemon_update.py
python3 scripts/test_desired_state.py
python3 scripts/test_handover_ota.py
python3 scripts/test_deployment_control.py
python3 scripts/test_telemetry.py
python3 scripts/test_logs.py
# Normal dev UID for these eight commands:
python3 scripts/test_cloudwatch_logs.py
python3 scripts/test_console.py
python3 scripts/test_rollout.py
python3 scripts/test_cadence_ota.py
python3 scripts/test_budget_ota.py
python3 scripts/test_fleet_settings_ota.py
python3 scripts/test_recovery_ota.py
python3 scripts/test_fleet_data_ota.py --binary suifleet-edge/target/debug/suifleet --shim suifleet-edge/target/debug/examples/grpc_sim_shim
```

## Measured artifacts and acceptance limits

- [1,000-target receipts](sf127-localnet.json): 188 measured report transactions,
  actual computation/storage/rebate/digests. First/last individual writes fronted
  10,515,200 / **10,585,200 MIST**, versus the 60,000,000-MIST device float.
- [Independent clients and oldest deletion path](sf127-clients-localnet.json):
  actual daemon/indexer and pre-identity compatible upgrade, not mocked success.
- [Full upgrade-security runner](sf127-upgrade-security-localnet.json): actual
  package/type origins, every transaction/abort, strict spending and cleanup.
  Final run used 592,084,532 MIST of the 600,000,000-MIST allowance, with about
  0.4980 SUI actual wallet spend. Funding/refunds did not replenish the allowance.

Initial test failures exposed native aggregate child-object limits and the copied
receipt rollout-authority gap; both were repaired and the affected meaningful
checks rerun successfully. The receipt population is 1,000, not 120,000. The
120,000 functional target follows from fixed-size roots and separate bounded
fields; no full-size concurrency/latency or public-network pricing claim is made.

No machine-session public-network transactions, physical programming, mainnet,
production provider credentials/purchases or manual real-wallet acceptance were
performed. The normal automatic public-testnet gates remain mandatory on the
pushed head/current base. Deferred final external acceptance remains in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).

## PR #301 independent-review repair — 2026-10-24

The reviewer reproduced an old-package handover bypass after catalog sharding:
inline schema 4 passed the published `schema_version != 2` guard. Catalog records
now retain inline schema 2 throughout the shard lifecycle. Directory presence
selects relocated group/binding fields and original payload semantics in Move,
the dApp, daemon (including final admission) and indexer. Ordinary/rollout records
continue using schema 4. Removal of catalog authority and final deletion remain
atomic after all roots have retired.

The snapshot base `b9e8053f4f034ca725686d19870cde6f3f756d63` was already an
ancestor; `git fetch origin main` and `git merge --no-edit origin/main` reported
`Already up to date.` No unrelated changes were integrated.

Repair verification (all listed final commands passed):

- `sui move test --path contracts`: **207 passed**, including verbatim legacy
  handover rejection, current handover rejection and full catalog-root cleanup.
- `python3 scripts/check_move_security.py`: pinned strict compiler/linters,
  unchanged diagnostic baseline, all package suites and deliberate bypass
  negative controls passed.
- `INSIDE_DEV_CONTAINER=1 make check-interfaces`: compiled ABI/layout and generated
  client consistency, independent wire vectors and frontend protocol/catalog/
  control/rollout/shard tests passed.
- `cargo +1.94.0 test --locked --manifest-path suifleet-edge/Cargo.toml`:
  **414 passed**, four existing OCI integration tests ignored.
- `cargo +1.94.0 test --locked --manifest-path suifleet-indexer/Cargo.toml`:
  **47 passed**.
- `cargo +1.94.0 build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples`
  and `cargo +1.94.0 build --locked --manifest-path suifleet-indexer/Cargo.toml`:
  passed; these rebuilt binaries were used in the client probe.
- `pnpm typecheck && pnpm build` in `dapp/`: passed, including static export.
- `python3 scripts/test_protocol_localnet.py --receipt-shards`: compatible
  base-to-head upgrade; actual published old-package catalog handover rejected
  before sharding, during populated migration, after readiness and during
  retirement; old catalog deletion, ordinary deletion and reporting rejected;
  frontend schema-2 relocated targets/receipts verified; 70 ordinary v2 receipts
  preserved; 1,000 targets reported with float-sized budgets; all roots cleaned.
  [Effects, aborts, digests and gas](sf127-localnet.json) record the final run.
- `python3 scripts/test_protocol_localnet.py --receipt-clients`: pre-identity
  deletion attack rejected; real daemon reported ordinary and schema-2 catalog
  v3 failures. The catalog journal error explicitly reached the configured
  missing-recipe HTTP endpoint, proving catalog authority resolution succeeded
  before the intentional download failure. The production indexer independently
  verified both receipts and the relocated catalog version binding. Full bounded
  cleanup passed. [Client evidence](sf127-clients-localnet.json).
- `python3 scripts/test_ota.py`, `python3 scripts/test_catalog_ota.py` and
  `python3 scripts/test_handover_ota.py`: all passed in one disposable root
  container with its own PID namespace. The handover fixture printed a transient
  HTTP BrokenPipe diagnostic during interrupted transfer and exited successfully.
- `git diff --check`: passed.

Move/frontend/localnet checks used `suifleet-dev:sf010-chrome`, pinned Sui 1.81.1,
Node 22.22.2 and pnpm 11.0.8, with network disabled. The Move dependency cache was
copied to `/tmp/sf127-fix-move-cache` and mounted writable for package locks;
Git history was mounted read-only for real base compilation. Initial invocations
with a read-only or absent Move cache failed setup and were corrected; the host
has no `pnpm`, so frontend verification used the pinned container. Rust commands
ran on the host with the locked dependencies and Rust 1.94.0. No public wallet
was mounted and no public-network writes were performed.

This repair reran the scoped checks above, not every historical check in the
initial evidence table. Hosted CI/public-testnet results remain pending and are
not assessed or claimed passed. The coordinator must obtain fresh independent
review and enforce exact-head/current-base CI before merging. No full 120,000-
device concurrency benchmark or physical/mainnet/provider/manual-wallet result
is claimed. Deferred acceptance remains in [#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [#159](https://github.com/tyler-suifleet/suifleet/issues/159).


## Automatic testnet allowance repair and current-base integration

The automatic run [40125390496](https://github.com/tyler-suifleet/suifleet/actions/runs/40125390496)
failed on head `30ba0e3` against base `b9e8053`. Its effects evidence records
341,773,600 MIST to publish the base and 377,802,963 MIST to upgrade the head.
After device setup, activation and funding, only 31,017,219 MIST remained of the
800,000,000-MIST allowance. The three-deployment transaction
`9Ri2vzRURkxxeTDt9bfqyiDG2SgyPh7xznKGsjkXQzpt` failed with `InsufficientGas`.
The unknown-result reservation correctly consumed the remaining allowance.

The package gate now defaults to a strict 1,000,000,000-MIST allowance; the CI
wallet wrapper passes that ceiling explicitly. Upgrade-security and wallet
allowances remain 700M and 150M, for a maximum 1.85 SUI across all three suites.
The 200M ordinary transaction cap, non-replenishing ledger, failed/unknown
charging, cache verification, dedicated wallet and one-faucet controls remain
in force. Offline regressions check CI arguments and replay the observed setup
costs followed by synthetic successful/unknown transactions. Synthetic costs
are ledger test inputs, not claimed public-chain measurements.

Current snapshot/base `c7548c4325a1a600bece25c8485af35faadd5361` was merged in
`59b8b1c`; it includes the reviewed Quilt changes. The package gate was also
executed on an isolated local chain through the wallet/localnet setup used in
`test_receipt_security_localnet.cjs`, substituting the driver command:

```bash
python3 scripts/live_package_upgrade.py --base-ref c7548c4325a1a600bece25c8485af35faadd5361 \
  --wallet TEMPORARY_LOCAL_WALLET --network localnet --walrus local \
  --state TEMPORARY_STATE --evidence TEMPORARY_EVIDENCE
```

It passed actual compatible upgrade/manifest verification, direct/group and
old-package reports by the real daemon, injected health failure and cleanup.
The ledger charged **818,891,984 MIST over nine operator transactions**, above
the former 800M ceiling and below the new 1B ceiling. This is a localnet
measurement with the pinned 1.82.1 transaction CLI, not a new public-testnet
result.

Current-base local checks used network-none `suifleet-dev:sf010-chrome`, pinned
compiler 1.81.1, Node 22.22.2/pnpm 11.0.8 and the populated Move cache. Host Rust
commands used 1.94.0 and locked dependencies. Results:

- `python3 scripts/check_move_security.py`: strict compiler/lints, reviewed
  diagnostics, authority negative controls; Move tests 207 + 61 + 22 passed.
- `INSIDE_DEV_CONTAINER=1 make check-interfaces`: compiled/generated ABI and
  independent wire/client/shard failure-path checks passed.
- `INSIDE_DEV_CONTAINER=1 make test-testnet test-tracking`: 81 testnet/toolchain,
  37 live-common and 10 tracking tests passed; authenticated cache negatives
  passed. Initial setup attempts mounted the Move cache at the wrong home and
  omitted the pinned transaction CLI; corrected mounts passed.
- `cargo +1.94.0 test --locked --manifest-path suifleet-edge/Cargo.toml`: 424
  passed, four existing OCI tests ignored. Corresponding indexer command: 47
  passed. Both binaries and edge examples rebuilt with locked dependencies.
- `pnpm typecheck && pnpm build` in `dapp/`: passed, production static export.

- `python3 scripts/test_protocol_localnet.py --receipt-shards`: current-base
  compatible upgrade, actual old-package attacks, preserved 70 v2 receipts,
  1,000 v3 receipts and complete cleanup passed. Largest individual report
  fronted 10,585,200 MIST, below the 60M float. Refreshed sanitized effects in
  [sf127-localnet.json](sf127-localnet.json).
- `python3 scripts/test_protocol_localnet.py --receipt-clients`: actual daemon
  ordinary/catalog v3 report effects, independent production-indexer binding
  and receipt reads, pre-identity deletion rejection and cleanup passed.
  Refreshed [client effects](sf127-clients-localnet.json).

The broader OTA run initially failed `test_budget_ota.py` with a recorded
`EOverAllowance` refusal; a serial rerun passed. The fixture had treated journal
admission as completed application while changing synthetic funding state on a
live daemon. The repair waits for the terminal Applied outcome, stops gracefully,
changes the allowance/balance fixture and restarts. An intermediate repair that
stopped on admission reproduced an indeterminate apply and failed queued-report
recovery; that failed attempt is not acceptance. Production funding behavior and
the simulator's no-refused-draw, no-new-work, queue and recovery assertions remain
unchanged.

The final repaired budget simulator passed twice, including zero refused draws.
All **25 Python commands** in the current `test-ota` recipe passed individually:
the 23 commands listed earlier plus the current-base compact-history variants
below. The first 15 ran as root in a disposable container with its own PID
namespace; CloudWatch and the suffix ran as the normal dev user. The initial
suffix stopped on the budget failure; the repaired budget/settings/recovery/data
suffix then exited zero. The root segment, compact/Quilt segment and earlier
CloudWatch/console/rollout/cadence commands also passed. This records equivalent
recipe-command coverage, not a successful monolithic `make test-ota` invocation.

```bash
node scripts/test_quilt_fixture_signer.cjs suifleet-edge/target/debug/examples/sign_quilt_fixture
python3 scripts/test_fleet_data_ota.py --binary suifleet-edge/target/debug/suifleet --shim suifleet-edge/target/debug/examples/grpc_sim_shim --compact-history
python3 scripts/test_fleet_data_ota.py --binary suifleet-edge/target/debug/suifleet --shim suifleet-edge/target/debug/examples/grpc_sim_shim --compact-history --quilt
```

The bounded fixture signer and both new archive modes passed. Simulator HTTP
BrokenPipe traces during interrupted downloads did not fail their enclosing
checks. `git diff --check` and JSON parsing of both refreshed effects files
passed. Proof scripts, pkcs11/OCI integration and hosted platform jobs were not
rerun locally for this allowance/fixture repair; earlier evidence is historical.

Hosted exact-head/current-base CI and automatic testnet remain pending after
push; this worker did not watch or poll hosted checks. Fresh independent review
and successful coordinator gates are still required. No public-chain mutations,
physical programming or deployment occurred. Deferred acceptance in #32/#159
remains unrun; the 120k storage-layout target is not a measured concurrency bound.


### Current-base synchronization after superseded testnet preflight

Automatic run [40136045525](https://github.com/tyler-suifleet/suifleet/actions/runs/40136045525)
failed at `Recheck head and base immediately before spending`: the controller
reported `superseded head/base: no testnet mutation submitted`. This is an
intentional stale-base refusal, not a receipt execution failure or successful
testnet acceptance. Its recorded base was `c7548c4325a1a600bece25c8485af35faadd5361`.

Merged snapshot/current main `233f4605e6d6e7c7c9f5c646e27d5528f001b948`
in `dfde18becc50d55549300d48ee8dcfc98360f09e`. The automatic merge changes only
coordinator dashboard/tests, coordinator documentation and tracking records;
receipt runtime, contracts, client code and testnet spending controls are unchanged.
The latest independent review reported no actionable source/security defect.

On the merged tree, dashboard regressions passed (45 tests),
offline testnet regressions passed (81 tests, one existing
skip), live-common regressions passed (37 tests), and tracking tests passed (10).
Synthetic connection-reset/cleanup diagnostics in live-common are expected
failure-path fixtures; no public-network transaction was submitted by these tests.
The previous broader receipt feature evidence above is historical and was not
rerun for this coordinator-only base synchronization. Hosted checks and automatic
testnet must run against the new head/current base; no pending check is approval.
Physical acceptance (#32), mainnet and final provider/manual-wallet acceptance
(#159) remain deferred and unrun. No deployment or hardware operation occurred.


### Current-base synchronization after Walrus publisher exhaustion

Automatic run [40140933114](https://github.com/tyler-suifleet/suifleet/actions/runs/40140933114)
used base `233f4605e6d6e7c7c9f5c646e27d5528f001b948` and head
`4e3c6140740b5659f21487271754c2de2ba64fe2`. Its failed-step log reports
successful cached-package verification, four independently effects-checked
receipt writes and deployment cleanup in the package-upgrade phase (allowance
used: 81,977,600 MIST). The upgrade-security phase then exhausted the existing
three-attempt publisher retry bound with HTTP 500 while uploading an artifact.
That phase recorded five transactions, 120,855,047 MIST allowance used and
approximately 0.0789 SUI net spend before failure. These log observations are
partial failed-run evidence, not successful full testnet acceptance. No new
public-chain transaction was submitted by this feedback worker.

The uploader already retries transient 5xx errors three times with bounded
backoff and fails closed on exhaustion; its existing offline regressions cover
transient recovery, exhaustion and immediate rejection of client errors. No
unbounded retry, spending increase or fabricated success was introduced.
Merged snapshot/current main `de238f73a6b12b9fccca8e257be6fc1a3b265714`
without conflicts in `bcb4b183724698294e9c8751d971f88a5a954959`. The base delta
contains testnet repair dispatch, coordinator regressions and documentation/
tracking only. Receipt runtime and spending controls are unchanged.

Scoped local checks on the combined tree: offline testnet regressions (81 tests,
one skip), live-common regressions (37 tests) and tracking tests (10 tests)
passed. Synthetic cleanup/connection-reset diagnostics are expected negative
fixtures. Broader Move/Rust/frontend/OTA/localnet receipt suites were not rerun
for this coordinator-only base integration; their earlier evidence is historical.
Hosted CI and automatic public-testnet must pass on the new head/current base,
followed by fresh independent review; this worker does not poll them. Physical
acceptance (#32), mainnet and final provider/manual-wallet acceptance (#159)
remain deferred and unrun. No merge, deployment or hardware operation occurred.

Full coordinator regression: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest
discover -s tools/pr-coordinator/tests` passed all 384 tests in 120.836s.
`git diff --check` passed.

### Repeated publisher failure: run 40148248434

[Automatic run 40148248434](https://github.com/tyler-suifleet/suifleet/actions/runs/40148248434)
used head `d16749b946c274f177ad3ab4a5e649c5f9b4ec6c` and base
`de238f73a6b12b9fccca8e257be6fc1a3b265714`. Its failed-step log confirms
chain `4c78adac` and the dedicated CI operator. The package-upgrade phase
reused verified packages, passed all four independently effects-checked receipt
writes and deleted the deployments: 76,971,332 MIST allowance used across
three operator transactions. The upgrade-security phase failed uploading the
legacy artifact at `Walrus.store` with HTTP 500 after bounded retries. Its final
log recorded five transactions, 120,855,047 MIST allowance used and approximately
0.0789 SUI net spend. These are partial failed-run observations, not full
acceptance; transaction digests are not present in this failed-step excerpt.

Added an offline regression specifically reproducing persistent HTTP 500: all
three PUT attempts fail, the final exception propagates, and a fourth successful
response is never consumed. No source defect was identified; changing retry
limits or substituting simulated storage would not establish actual public
Walrus acceptance. The provider failure remains unresolved, so this repair
handoff is blocked rather than a claim of a fixed service or successful CI.
No automatic rerun or public-wallet mutation was initiated by this worker.

`git merge --no-edit origin/main` reported already up to date; the snapshot and
fetched current base above are included. Scoped checks: `python3 -m unittest
discover -s scripts/tests -p 'test_live_common*.py'` passed 38 tests;
`python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'` passed
81 tests with one existing skip; `git diff --check` passed. Expected synthetic
connection-reset/cleanup diagnostics come from negative fixtures. Broad receipt,
Move/Rust/frontend/OTA/localnet suites were not repeated for this test/documentation
change. Hosted checks on the pushed head remain pending/unassessed; this worker
will not watch them. Next: restore publisher availability or implement a separately
verified bounded provider repair, then obtain actual exact-head/current-base
CI/testnet success and fresh independent review. Physical #32 and mainnet/final
provider/manual-wallet #159 acceptance remain deferred and unrun.


### Current-base feedback handoff (2026-10-24)

The latest independent review of `74f88c5` found no actionable source/security
defects and confirmed the old-package handover repair remains resolved. Its
acceptance verdict was blocked: hosted checks were incomplete and automatic
testnet evidence was missing after the previously recorded publisher failures.
This is not a newly identified receipt-runtime defect or a provider repair.

Merged snapshot/current fetched main `c598629ea329062e08afa0e019b7b0d82eb191f0`
without conflicts. The incoming changes concern coordinator repair dispatch and
tracking; the receipt runtime is unchanged. Local checks after integration:
`python3 -m unittest discover -s scripts/tests -p 'test_live_common*.py'`
passed 38 tests; the equivalent `test_testnet*.py` command passed 81 tests with
one existing skip. Expected cleanup/reset diagnostics are negative fixtures.
Broader Move/Rust/frontend/OTA/localnet checks were not repeated for this merge.

Hosted exact-head/current-base CI and automatic public-testnet checks remain
pending/unassessed at handoff; this worker does not watch or poll them. No public
mutations, provider substitutions, retry increases, deployment, hardware work or
merge to main were performed. A successful actual automatic testnet run and fresh
independent evidence review remain required. Deferred external acceptance remains
unrun in #32 and #159. Coordinator-only integration and all software/security gates
remain in force.

Full post-merge coordinator regression: `python3 -m unittest discover -s tools/pr-coordinator/tests` — 391 tests passed in 122.116 seconds. `git diff --check` passed.

### Current-base rollout-health integration (2026-10-25)

The feedback snapshot has no outstanding actionable source finding, but requires
base `40d104f6c2e6ccf1c4ce8aa6297ca8ad9b89911d` (SF-138 on-chain rollout health).
The merge preserves both optional introduction-origin groups and both signer
negative suites. The wallet settles authoritative shard outcomes before calling
`advance_rollout_onchain`; it never substitutes creator/HTTP health. The daemon
resolves the original payload schema from the receipt directory before sending
post-apply health, rather than silently skipping inline-schema-4 rollouts.

Three added Move cases exercise sharded healthy completion, degraded abort and
revoked-identity rejection after receipt settlement, including bounded cleanup.
The frontend regression checks automatic sharding, settlement-before-advance and
migration/retirement refusal. The actual-daemon/indexer localnet probe now also
requires a schema-4 rollout receipt, explicit settlement, a real device health
retry and durable health completion. A Failed missing-recipe result must produce
an authenticated unhealthy sample; this does not claim successful installation.

Verification uses the pinned `suifleet-dev:sf037` compiler (1.81.1), Rust 1.94.0
with locked dependencies, and network-none disposable local chains with temporary
identities. No shared/public wallet was mutated. Hosted exact-head/current-base
CI and automatic testnet remain pending/unassessed; this worker does not poll
them. Fresh independent review is required before coordinator integration.

Local checks on the merged runtime and generated interfaces:

- `sui move test --path contracts`: 215 passed, including all three combined
  sharded rollout-health cases.
- `python3 scripts/check_move_security.py`: strict compiler/linter baselines and
  deliberate bypass negatives passed; package suites 215/215, 61/61 and 22/22.
- `make check-interfaces`: compiled ABI, generated clients, ten ABI/stale-file
  negatives, independent wire vectors and frontend recipe/catalog/control/
  rollout/shard regressions passed.
- `cargo +1.94.0 test --locked --manifest-path suifleet-edge/Cargo.toml`: 437
  passed, four existing OCI cases ignored; `--features pkcs11`: 439 passed with
  the same four ignored cases. Binaries/examples rebuilt with locked dependencies.
- Indexer locked tests: 47 passed on retry; initial run had one timestamp-order
  assertion failure in `telemetry_routes_authenticate_by_signature_and_token`
  (`server_time_ms >= now`). No indexer source was changed to hide it. The actual
  indexer binary was rebuilt for the client probe.
- dApp `pnpm typecheck && pnpm build` in the pinned dev image: passed.
- Host offline regressions: live-common 45 passed; testnet 117 passed with one
  installed-toolchain skip; manifest 18 passed; authenticated Node cache checks
  passed. Setup attempts to generate interfaces on the host (no `sui`) and run
  the container OTA target without its Rust registry cache failed before their
  intended checks; the successful commands use the configured pinned toolchain.
- `python3 scripts/test_protocol_localnet.py --receipt-shards`: passed against
  current base `40d104f6c2e6ccf1c4ce8aa6297ca8ad9b89911d`, preserving 70 v2 receipts,
  exercising real old-package attacks and complete cleanup, and populating
  1,000 receipts. Maximum single-report gross 10,595,200 MIST is below the
  60,000,000-MIST float. An earlier run also passed against base `58fce5b`;
  the versioned JSON records the current-base run.

The client fixture now waits for `discovery_cursor`, since `initialized` is
persisted before baseline/fence setup finishes. Two runs using the earlier
readiness signal timed out with no discovered deployment; one earlier run
completed the full sharded health path. The final frozen-source evidence below uses
the strengthened readiness fixture, including independent health effects.
The local security fixture explicitly uses the same 700,000,000-MIST ceiling as
`ci_testnet_wallet.py` for upgrade-security. Its previous default 600,000,000-MIST
attempt stopped conservatively at 598,394,064 MIST before cleanup. Neither the
live helper's default nor public CI ceilings, accounting or uncertainty handling
was changed. Disposable localnet funding is not public spending evidence.

The frozen security probe passed the complete `--receipt-security` path using
that 700,000,000-MIST local ceiling: real compatible upgrade, old-package and
impersonation attacks, populated v2 migration, daemon Applied reporting, durable
v3 retry without updater replay, revocation refusal and bounded cleanup.
Its 41 transactions used 599,496,132 MIST of non-replenishing allowance and
0.5054 SUI net; see `sf127-upgrade-security-localnet.json`. The lower final usage
does not invalidate the prior stop: the ledger must reserve the full next budget
before submission, not assume its eventual smaller gas cost.

The final `--receipt-clients` run passed on the frozen strengthened fixture:
actual old-package deletion rejection, ordinary and schema-2 catalog v3 reports,
schema-4 rollout receipt settlement, device health retry and durable completion,
independently read successful health execution effects, production indexer
receipt/binding reads and complete cleanup. `sf127-clients-localnet.json` includes
the signed sample and its independently verified transaction digest. The probe
retains the historical pre-identity base intentionally; the separate 1,000-target
probe above verifies the current-base upgrade pair.

OTA execution context matters: the host `make test-ota` passed its earlier cases
then stopped because the non-root supervisor scanner could not inspect
`/proc/1/cwd`. In a disposable root, network-none container, `make test-ota`
rebuilt the debug binaries/examples and passed every command from the fixture
signer through `test_logs.py` (including supervision, configuration, daemon
handoff, desired state, handover, controls and telemetry). It then stopped at the
CloudWatch adapter's deliberate refusal to run as root. No guard was weakened.
The remaining exact target commands, from `test_cloudwatch_logs.py` through all
four `test_fleet_data_ota.py` modes, run as the normal user in a disposable
network-none container with `set -eu`. This is split-context verification of the
target recipe, not a claim that a single `make test-ota` invocation passed.

Main subsequently advanced to `3c6625c33b238914a2b22d4b7c1a3dd1a69aff27` with
coordinator repair-dependency logic, tests and tracking only; it was merged in
`51bf2dcb`. `git diff --quiet 40d104f origin/main -- contracts suifleet-edge
suifleet-indexer dapp interfaces scripts` confirmed the entire application,
contract and OTA-harness source is unchanged between those bases. The versioned
current-base 1,000-target receipt therefore tests identical base bytecode; no
identical package needs to be republished. Coordinator regressions cover the
incoming coordinator delta separately.

`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s
tools/pr-coordinator/tests` passed all 396 tests in 78.388 seconds on the combined
`3c6625c3` base, using offline temporary fixtures/databases and real-process
regressions. No installed coordinator service or production database was changed.

The normal-user, network-none OTA tail exited 0 with `set -eu`: CloudWatch,
console, three-daemon rollout health retries, cadence/fallback, budget, fleet
settings, recovery and all four fleet-data modes (normal, idle reads, compact
history and compact history with Quilt) passed. Combined with the root prefix,
every `test-ota` recipe command was verified successfully in its required user
context. No successful single-invocation full-target result is claimed.

Final limits: the 120,000 receipt target is a bounded-storage design, not a load
result. V3 status reports use independent roots, while staged-rollout health
still mutates the bounded coordinator and keeps the 512-member rollout limit.
No public-network writes or hardware operations occurred. Hosted CI/automatic
testnet and fresh independent exact-head/current-base review remain required
coordinator gates. Physical acceptance stays unrun in #32; mainnet and final
production-provider/manual-wallet acceptance stay unrun in #159.

### CI estimator synchronization repair (2026-10-25)

CI run `40182529849`, job `121136416089`, failed at
`test-fleet-settings`: the refreshed 1,000-target receipt evidence records
1,080,000 MIST computation, while the estimator retained 1,070,000 MIST.
`node scripts/test_fleet_budget.cjs` reproduced the exact mismatch locally.
The estimator now uses the recorded maximum single-report effects, preserving
9,515,200 MIST storage and 4,378,968 MIST rebate. The independently asserted
Mainnet-rescaled net estimate is consequently 5,244,232 MIST. The evidence
comparison and all pricing, deposit and transaction checks remain enforced.

Verification for this repair:

- `INSIDE_DEV_CONTAINER=1 CHROME_EXECUTABLE=/opt/google/chrome/chrome make test-fleet-settings`
  passed all four scripts, including the real-browser settings flow (host).
- In `suifleet-dev:sf037`, network disabled, the budget, settings and settings-cost
  Node scripts passed with pinned Node 22.22.2.
- In the same pinned image, `cd dapp && pnpm typecheck && pnpm build` passed.
- `git diff --check` passed. No contracts, daemon or transaction logic changed;
  their suites and live tests were not rerun for this numeric-input repair.

The branch already contains snapshot/fetched base
`3c6625c33b238914a2b22d4b7c1a3dd1a69aff27`; merging it was a no-op.
Hosted CI and automatic public-testnet checks on the repaired head remain pending
for the coordinator, together with fresh independent review. This is no approval
or network acceptance claim. Physical acceptance remains deferred in #32;
mainnet and final provider/manual-wallet acceptance remain deferred in #159.


### CI poll-read measurement repair (2026-10-25)

CI run `40182958348`, job `121137937172`, reached the final queued-receipt
idle assertion with three batch reads plus one service-info and five object
reads. The earlier deliberately unknown-schema record had a delivered receipt
but still-pending post-receipt health: schema resolution correctly fails closed
and retains its retry. That independent retry can overlap the following idle
measurement. Local runs of the original harness passed, including controlled
scheduling variations; the hosted failure is timing-dependent.

The harness now explicitly verifies that the unknown schema retains a slow
health retry, restores the fixture to a readable ordinary schema, and waits for
actual health settlement before measuring idle reads. The rejected status and
zero applies are still required, as are exactly three batch reads and zero other
reads for both idle measurements. No daemon retry behavior or assertions were
relaxed. This isolates receipt backoff from a distinct health retry.

Verification for the poll-read repair:

- `docker run --rm --network none -v "$PWD:/workspace" -v /home/tyler/.cargo:/home/dev/.cargo -w /workspace -e CARGO_NET_OFFLINE=true suifleet-dev:sf037 bash -lc 'INSIDE_DEV_CONTAINER=1 make test-ota'`
  passed (exit 0), including the locked daemon/examples build, receipt idle-read
  assertions, all OTA/lifecycle/control/recovery scenarios and all four fleet-data
  modes. This uses real local daemon/shim processes and simulated chain/Walrus.
- `python3 -m py_compile scripts/test_ota.py` and `git diff --check` passed.
- A direct host `make test-ota` first needed `RUSTUP_TOOLCHAIN=1.94.0`; with
  that pin it passed the repaired receipt check but stopped in the supervisor
  harness because the non-root host user cannot inspect `/proc/1/cwd`. The
  isolated container run above passed that supervisor check and the entire suite.
  An initial container attempt without dependency caches failed offline dependency
  resolution; the successful command mounts the existing cache and enforces offline
  Cargo resolution. No check was skipped or weakened.

Snapshot and freshly fetched current base remain `3c6625c33b238914a2b22d4b7c1a3dd1a69aff27`;
`git merge --no-edit origin/main` was already up to date. No public-network
mutations or live/hardware tests were performed. Hosted exact-head/current-base
CI and automatic testnet acceptance remain pending for the coordinator after
push, along with fresh independent review. Deferred acceptance remains unrun in
SF-031/#32 and SF-087/#159. This repair is not approval.

## Current-base integration and client-fixture repair (2026-10-25)

Integrated main `82591d81a130e8eb03f2e176590cf991347aed30` (SF-139 opaque
storage) into the owner branch. The only manual merge resolutions preserve both
SF-127/SF-139 acceptance sections and policy entries, and retain the existing
bounded persistent-publisher-failure test. No runtime or contract behavior was
changed by the resolutions. The latest independent snapshot review found no
unresolved source/security defect; hosted checks were pending and the newer base
was missing, so that review remains source evidence rather than approval.

The refreshed client harness exposed two timing assumptions: its 60-second wait
could expire at the default 60-second event discovery tick, and a health object's
previous-transaction digest could appear before transaction indexing. The local
fixture now explicitly configures one-second discovery and waits for indexing
before independently checking health effects. It retains all report, identity,
catalog, rollout health, indexer and cleanup assertions. The first attempt failed
at daemon reporting; the second reached health but returned transaction NotFound;
the corrected rerun passed. These findings are addressed within SF-127/PR #301.

Executed on the merged tree, in disposable `suifleet-dev:sf037` containers with
`--network none` (Cargo cache mounted with `CARGO_NET_OFFLINE=true`; Move cache
mounted for compilation; repository metadata mounted read-only for localnet
base archives):

- `INSIDE_DEV_CONTAINER=1 make test-contracts check-interfaces dapp-typecheck dapp-build`
  passed: Move 215 core / 61 storage / 29 fleet-data tests, compiled ABI checks
  (776 references, zero drift), generated interfaces, independent wire/frontend
  fixtures, frontend typecheck and production static build.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p test_live_common.py`
  passed 45 tests, including bounded read retry and persistent publisher failure.
- `python3 scripts/test_protocol_localnet.py --receipt-shards` passed compatible
  upgrade, real old-package attacks, 70 preserved v2 receipts, all 1,000 v3
  receipts and complete bounded cleanup. Refreshed effects are in
  [sf127-localnet.json](sf127-localnet.json): maximum individual gross
  10,595,200 MIST < 60,000,000 MIST float. Computation 1,080,000, storage
  9,515,200 and rebate 4,378,968 match the existing client cost model.
- `python3 scripts/test_protocol_localnet.py --receipt-clients` passed actual
  daemon ordinary/catalog reports, device-signed rollout health with independent
  successful effects, production-indexer binding/receipt reads and full cleanup.
  Refreshed effects are in [sf127-clients-localnet.json](sf127-clients-localnet.json).

The first uncached Move invocation failed resolving dependencies with networking
disabled; rerunning with the existing Move cache passed. The first localnet
invocation lacked the linked worktree's Git metadata mount and failed before
publication; rerunning with read-only metadata completed. These setup failures
are not test passes. Local runtime suite completion is recorded below.

No public wallet mutations, production deployment, hardware or mainnet actions.
Hosted exact-head/current-base ordinary/security CI and automatic testnet remain
pending/unassessed at repair handoff; no hosted CI polling. Fresh independent
review and coordinator merge gates remain required. Physical acceptance remains
unrun in SF-031/#32; mainnet/final provider/manual-wallet acceptance remains unrun
in SF-087/#159. The 120,000 figure remains a storage bound, not a load benchmark;
reports serialize within each root and migration/cleanup remain operator-funded.

Runtime completion:

- `INSIDE_DEV_CONTAINER=1 make edge-test test-ota test-opaque` completed both
  locked Rust suites: 447 default and 449 PKCS#11 tests passed, four existing OCI
  integration tests ignored in each. It rebuilt all daemon bins/examples and
  passed the entire `test-ota` target, including both strict three-batch receipt
  idle checks, rollout health retries and all four fleet-data modes. Opaque SDK
  unittest also passed (one test). The combined command then exited 2 because
  the opaque IPC admin fixture requires root and this container ran as `dev`;
  that final precondition failure is not counted as an opaque runtime pass.
- Logs are local `/tmp/sf127-base-integration.log`, `/tmp/sf127-base-static.log`,
  `/tmp/sf127-base-localnet.log` and `/tmp/sf127-base-clients.log`. They are not
  public artifacts; sanitized effects are committed instead.
- Corrected opaque runtime invocation: `docker run --rm --network none --user
  0:0 -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/workspace" -w /workspace
  suifleet-dev:sf037 python3 scripts/test_opaque_ota.py --binary
  suifleet-edge/target/debug/suifleet --shim
  suifleet-edge/target/debug/examples/grpc_sim_shim` passed (exit 0). No test or
  fixture precondition was bypassed. `git diff --check` passed. Freshly fetched
  main is still `82591d81a130e8eb03f2e176590cf991347aed30` and is an ancestor of
  the repair head.


### Data browser base integration (2026-10-25)

The latest independent source review of `4da4c622` found no actionable defects.
The new snapshot base `9b3799fe3838d6b4e94bd116f4ecbe181b8294c4`
adds the reviewed SF-140 Data browser. It merges without conflicts; contracts,
shared interfaces, daemon, indexer and receipt harnesses are unchanged by this
integration. No prior runtime or local-chain result is claimed as a new run.

Fresh checks on the merged tree:

- Network-none `suifleet-dev:sf037`, with the existing Move cache mounted:
  `INSIDE_DEV_CONTAINER=1 make check-interfaces` passed: 776 client references,
  14 compiled modules, zero drift; generated ABI and receipt-shard fixtures pass.
- Network-none `suifleet-dev:sf010-chrome`, with `/opt/sui-network` copied from
  `suifleet-dev:sf037` and mounted read-only: frontend verification uses
  `INSIDE_DEV_CONTAINER=1 make dapp-typecheck dapp-build`. Its prerequisite
  offline suites include 117 testnet-toolchain tests, 45 live-common tests,
  18 manifest tests and the Data unit/real Chromium browser flows.
- Initial setup attempts lacked the Move cache, Chrome or the pinned network
  CLI respectively. These are setup failures, not passes; no check was weakened.
  Any automatically generated wallet was disposable, unfunded container state.
- Local logs: `/tmp/sf127-data-base-retry.log` and
  `/tmp/sf127-data-frontend-final.log`. No public-wallet mutations.

Hosted exact-head/current-base CI and automatic public-testnet acceptance remain
pending/unassessed at handoff; no CI watch or polling. Fresh independent review
and coordinator-only merge remain mandatory. Physical acceptance remains unrun
in SF-031/#32; mainnet and final provider/manual-wallet acceptance remain unrun in
SF-087/#159. The 120,000 figure is a storage bound; reports serialize within a
root and migration/cleanup remain operator-funded.

Completion: the combined frontend command above exited 0; all prerequisites,
TypeScript typecheck and the production static build passed. `git diff --check`
also passed. The fetched base is an ancestor of the integrated head.


### Device inbox base integration (2026-10-25)

Latest independent source review of `b57519f9` found no actionable defects.
Integrated snapshot/current main `f076a6334d75539ef3ba447484924cfa2896cb38`
(SF-125 device inboxes/member index) in `7e4d2d9b`. Conflict resolutions retain
both optional origin groups and their all-or-none validation, initialize shard
roots before announcing the same deployment/rollout record, and retain both
interface fixture suites. Added combined-feature frontend PTB regressions and
a Move regression proving group/device announcements and scheduled starts work
after group targets move behind the receipt barrier.

Fresh local checks (disposable network-none containers):

- `INSIDE_DEV_CONTAINER=1 make test-contracts generate-interfaces check-interfaces`
  passed 235 core / 61 storage / 29 fleet-data tests and 847 client references
  against 15 compiled modules with zero drift. After adding the combined Move
  regression, `sui move test --path contracts --build-env testnet` passed 236/236.
- `INSIDE_DEV_CONTAINER=1 make dapp-typecheck dapp-build` passed, including
  offline toolchain/live-common/manifest fixtures, actual Chromium flows,
  combined inbox/shard transaction fixtures, TypeScript and static production build.
- `INSIDE_DEV_CONTAINER=1 make test-interfaces test-compat test-publish` passed:
  17 interface, 15 compatibility, 40 Greengrass compatibility and 36 publishing
  tests. Initial invocation omitted the Move cache and failed before its
  compilation-dependent test; corrected invocation mounted the existing cache
  and passed without weakening checks.

Runtime and isolated-chain completion is recorded below. Local logs are
`/tmp/sf127-inbox-{static,frontend,runtime,localnet,compat-retry,security}.log`.
No public-wallet mutations or hosted CI polling. Hosted exact-head/current-base
ordinary/security CI and automatic testnet remain coordinator gates, pending on
the pushed repair head. Fresh independent review is required. Physical acceptance
remains unrun in SF-031/#32; actual mainnet and final provider/manual-wallet
acceptance remain unrun in SF-087/#159. Reports serialize within each root;
120,000 is a storage bound, not a concurrency benchmark. Migration/cleanup
remain operator-funded.

Additional completed checks:

- `python3 scripts/check_move_security.py` passed pinned compiler/strict linters,
  compatibility baselines, genuine authority-negative tests and bypass mutations,
  including the new inbox authority/member-index protections.
- `make edge-test` passed 456 default and 458 PKCS#11 tests; four existing OCI
  tests ignored in each. Both builds used locked dependencies.
- Integration measurement found the max individual report computation increased
  to 1,160,000 MIST (storage 9,515,200; rebate 4,378,968; gross 10,675,200).
  Updated `MEASURED.receipt` and the independent rescaled-price expectation;
  the final `make dapp-typecheck dapp-build` rerun passed after this correction.
  The earlier localnet run used base `9b3799fe`; it is superseded by the
  current-base run recorded below, not mislabeled as current-base evidence.

- `python3 scripts/test_protocol_localnet.py --receipt-shards` passed against
  current base `f076a6334d75539ef3ba447484924cfa2896cb38`: compatible upgrade,
  old-package report/delete/catalog handover attacks, resumable migration of
  70 v2 receipts, 1,000 authoritative v3 receipts and complete bounded cleanup.
  Final single-report gross 10,675,200 MIST < 60,000,000 MIST float.
  Refreshed sanitized effects are in `sf127-localnet.json`. Final budget/settings
  fixtures were rerun after this evidence refresh and passed.

- Rebuilt the production indexer with locked dependencies, then
  `python3 scripts/test_protocol_localnet.py --receipt-clients` passed:
  pre-identity old-package deletion rejection, actual daemon ordinary/catalog
  v3 reports, independently effects-checked device-signed schema-4 rollout health
  retry, production-indexer receipt/binding reads and bounded cleanup. Refreshed
  sanitized effects are in `sf127-clients-localnet.json`.

Final base movement: main advanced to
`2169f7979c1c446b838f04a46e05e31a168697d6` during runtime verification.
Integrated it conflict-free in `61fab74f`; the delta is SF-151 coordinator
configuration and tracking/docs only. `git diff --quiet f076a633 origin/main --
contracts dapp suifleet-edge suifleet-indexer interfaces scripts Makefile` passed,
so the recorded application/chain checks cover byte-identical code on this base.
`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s
tools/pr-coordinator/tests -p test_repair_budgets.py` passed all five focused
coordinator regressions. No installed services or coordinator database changed.

Runtime completion: `INSIDE_DEV_CONTAINER=1 make edge-test test-ota &&
cargo test --locked --manifest-path suifleet-indexer/Cargo.toml && cargo build
--locked --manifest-path suifleet-indexer/Cargo.toml` exited 0. The full OTA
target rebuilt all daemon bins/examples and passed every scenario, including
strict receipt idle reads, inbox/pruned-history safety-net behavior, catalog,
dependencies, firmware restart, supervisor, configuration, daemon update, desired
state, handover, deployment controls, telemetry/logs/console, rollout, cadence,
budget/settings, recovery and all four fleet-data modes. Indexer unit tests
passed 48/48 and its locked build passed. These are simulator/isolated-chain/host
checks; they establish no physical, public-provider or real-wallet acceptance.
Final `git diff --check` passed. All test/build resource locks were released.

## Package reservation repair (2026-10-25)

Automatic testnet run [40206647454](https://github.com/tyler-suifleet/suifleet/actions/runs/40206647454)
failed upgrading `a3f4504f` over `2169f797`: the fixed 400M-MIST package
transaction budget was exhausted. Its public evidence records a 386,836,000-MIST
base publish and 396,504,956 MIST settled before the upgrade. A read-only gRPC
lookup independently confirms failed digest
`9naKZ55speAiQHxvKNmWJwFsRbaZc2sCVhGkJcyPu2b7`, `InsufficientGas`, checkpoint
399098901, and 3,900,163 MIST net gas. No machine-session public-wallet mutation
was made. The existing private checkpoint and historical quarantine are preserved
for normal effects reconciliation; no uncertain transaction was discarded.

The package reservation is now 500M MIST, capped by the unchanged 1.0-SUI suite
allowance. Funding preflight requires a gas pool covering that reservation;
unknown outcomes still consume their full reservation. Fresh checks:

- Disposable `suifleet-dev:sf037`, network none:
  `make INSIDE_DEV_CONTAINER=1 test-testnet` passed: 118 testnet/toolchain/
  reconciliation tests, 45 live-common tests, authenticated-cache fixtures.
  The initial host invocation failed only its installed-toolchain check because
  the host has no `sui`; the pinned-container invocation passed.
- `python3 scripts/test_protocol_localnet.py --receipt-shards` passed in the
  disposable network-none container with the pinned Move cache, as user `dev`.
  The compatible upgrade now explicitly uses a 500M-MIST budget. It preserved
  70 legacy receipts, passed old-package attacks/barriers, populated 1,000
  authoritative receipts and completed bounded cleanup. Refreshed
  [raw evidence](sf127-localnet.json) records base `2169f797`, the actual digests
  and maximum individual receipt gross of 10,675,200 MIST. Initial setup attempts
  lacked the linked Git metadata or mounted the cache under the wrong home/user;
  they failed before the tested upgrade, and the corrected run passed.
- `node --check scripts/test_receipt_shards_localnet.cjs` and `git diff --check`
  passed. Current/snapshot base `2169f797` is already an ancestor; merging
  `origin/main` reported “Already up to date.”

This is repair evidence, not approval or successful public-testnet rerun evidence.
Hosted exact-head/base CI and automatic testnet remain pending; fresh independent
review is required. No hosted CI polling/watch or manual rerun was performed.
Physical acceptance remains in SF-031/#32; mainnet and final provider/manual-wallet
acceptance remain in SF-087/#159, explicitly unrun.
