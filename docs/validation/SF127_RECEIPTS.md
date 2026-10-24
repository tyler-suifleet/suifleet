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
