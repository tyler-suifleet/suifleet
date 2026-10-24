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
