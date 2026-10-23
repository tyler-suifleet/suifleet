# SF-090 candidate pilot experiment

Status: **draft validation, no supported paid-pilot capacity established**.
Codex configured/running model identifier: `gpt-6.1-sol`; no finer provider build
identifier is exposed. Producer pin:
`81a4d2e6e8b94a62b7e1b682ba9bdf115367e634` (SF-082/#160).
The pin is unmerged. These results must not be presented as final integrated
acceptance or as an availability/SLA claim.

The workloads were declared before their measurements in `workload.json`,
`isolated-workload.json` and `public-read-workload.json`. The candidate comprises
128 synthetic readers/signing identities, four concurrent consumers, three rounds
with 16 KiB / 256 KiB / 1 MiB log payloads; hourly health and daily archives;
three purchased retention epochs; a 100-device browser index page bound. JSON,
base64 and Seal overhead are additional to payload bytes. Archive record counts
are payload/1024, up to 1024. The isolated-chain sample has 16 enrolled devices,
four concurrent writers and a 30-second stage p95 guard. The public sample is
**eight read-only effects requests**, concurrency two, 10-second read p95 guard,
zero new spend and zero writes. Provider quotas remain explicitly unquoted/null.

The process RSS ceiling is 1 GiB. Crypto/fetch/health guards are 5 seconds p95;
index/browser-page recovery is 1 second p95. Five browser rounds also have a
5-second initial-render guard. Nearest-rank p50/p95 values are regression guards
on this machine, not population estimates or provider promises. There is no
customer agreement or production quota inferred from this candidate profile.

## Measured boundaries

| Experiment | What actually runs | What it cannot establish |
|---|---|---|
| `fixture-run.json` | 128 different Ed25519 signing keys, 384 actual SDK Seal encryptions, shipped verified health/archive/index readers, independent AES-GCM opening, real signatures/digests/bindings | Chain consensus, real provider authorization, certification or fleet availability |
| `isolated-run.json` | Fresh published sources, 16 activated/enrolled devices, concurrent prepaid gas draws, signed health anchoring, direct browser-reader visibility, verified authoritative receipts, one successful/three refused concurrent storage registrations | Device updater application, certified storage-node throughput or public pricing |
| `browser-run.json` | Shipped FleetDataPage in real Chromium; real BCS/type/binding verification; 100-to-128-device pagination; throttling error and recovery | Extension wallets, real signed Seal sessions, archive-rendering scale |
| `daemon-run.json` | Four native archive/restart drills, concurrency two; real native signatures/ciphertext and browser verification; exhausted storage, ambiguous/pruned history, restart, diagnostics/crash cadence | A shared-chain four-device fleet or a long-running soak |
| `public-read-run.json` | Fresh independent Testnet reads of eight historical health/storage/anchor effects; gas totals matched against producer evidence | A new native upload, new measured enrollment/finalization latency or provider load |

The chain experiment publishes instrumented **test-only** upstream Walrus
initialization (one deterministic committee, synthetic WAL/rates) into a disposable
network-none chain. Its mutation/accounting code remains upstream. It uses the
on-chain encoding formula to buy exactly one small blob's storage. Failed
registrations must abort in `fleet_storage::take_storage`; the independently read
storage count must be one. Budget tests submit fully resolved transactions so the
second draw's quota refusal is a real failed effect, not a dry-run exception.
All successful and refused transaction digests and gas summaries are retained.
No contract source, production API, ABI or deployment manifest was changed.

The crypto experiment's injected RPC, gateway, keyserver and delayed-index outages
must exhaust exactly three **driver** attempts, then recover after fault removal.
The reconnect experiment schedules 128 consumers through a four-operation driver
pool. This measures the experiment scheduler, not a daemon/browser retry policy.
RPC failure must yield unavailable health; foreign fleet/digest substitutions must
fail before requesting keys. Integrity/authorization/exhaustion failures are never
blindly retried. Failed worker pools drain existing work and stop scheduling.
Native recovery drills separately check pending-purchase preservation and no
duplicate registration. No test equates a synthetic receipt with updater success.

## Reproduction

Use the locked dependencies from this checkout (`pnpm install --frozen-lockfile`
in `dapp/`) and pinned dev toolchain. Inside the dev environment:

```bash
node scripts/test_pilot_load.cjs
node scripts/test_fleet_data.cjs
node scripts/test_fleet_bulk.cjs
make INSIDE_DEV_CONTAINER=1 pilot-fixtures
CHROME_EXECUTABLE=/opt/chrome-headless-shell/chrome-headless-shell \
  make INSIDE_DEV_CONTAINER=1 pilot-browser
# Only in a disposable Docker --network none container, with empty private
# wallet state, pinned Move cache and the built chain_facts readiness example:
make INSIDE_DEV_CONTAINER=1 pilot-localnet
# Networked, read-only; mounts/loads no wallet and spends no gas:
make INSIDE_DEV_CONTAINER=1 pilot-public-read
python3 scripts/pilot_daemon_soak.py \
  --binary suifleet-edge/target/debug/suifleet \
  --shim suifleet-edge/target/debug/examples/grpc_sim_shim \
  --receipt /tmp/suifleet-pilot/daemon.json
```

Make receipts default to `/tmp/suifleet-pilot`; override `PILOT_EVIDENCE_DIR`.
The local chain runner verifies compiler/network CLI checksums/versions, rejects
non-loopback network interfaces and existing loopback chain ports, starts fresh
private node state and terminates the node on success/failure. It never loads a
public wallet. Run current-source build prerequisites using the normal locked
`cargo build --bins --examples` before independent reproduction. The recorded
native runs reused already-built producer binaries; their exact hashes are in
the receipt, and no fresh Rust build is claimed. Runtime logs/keys remain private
temporary files and are removed, not copied to evidence.

Actual environment: Linux `6.6.87.2-microsoft-standard-WSL2`, Node `22.22.2`;
Sui compiler `1.81.1-e04cf9aa915c`, network-none node `1.82.1-987123842623`;
Chromium version is recorded in its receipt. Container image IDs:

- `suifleet-dev:sf037`: `sha256:31f727258ccd2eb7d00039c26bca6cf045d6d71e4d19fe51ac899f4f239e5b5b`.
- `suifleet-dev:sf010-chrome`: `sha256:2d0efd8a78b35eda30ef940c64c5e387dd15663a84a824ccc4f420342a030568`.

The execution commands used Docker `--network none` for all fixture, browser,
chain and native drills; only the eight-request public readback had networking.
Frontend dependencies and existing native binaries were mounted read-only from
the producer worktree/cache. The shared `build:suifleet` lease covered actual
Move compilation and was renewed/released by the attached parent process.
No service installation, network mutation lock, wallet write or hardware action
was needed. Receipt revisions identify the actual source snapshots; later
report/documentation commits do not relabel old runs as new measurements.

## Failures and remaining gates

Early experiment attempts failed and were repaired: missing frontend dependency
mount, inaccessible worktree Git metadata at report time, SDK effects using
`gasUsed` rather than transport `gasSummary`, and a nominal storage pool that
actually fit four small blobs. The final pool uses the chain's exact encoding
formula, with unchanged pass criterion (one success). Those failed local attempts
are not Testnet spending or passing evidence. Their logs remain under private
`/tmp/sf090-*`; no raw daemon/chain logs are published.

The initial `make test-tracking` run had 1 failure and 1 error of 10;
the bootstrap dry run rejected the missing SF-083 dependency. On 2026-10-22,
under explicit restoration authorization, only existing published SF-083/#161
and its prerequisite SF-088/#171 were restored from the audited canonical entries.
Their issue IDs, node IDs, closed titles and Project-item issue identities were
verified against GitHub before restoration. No issue was created and no
acceptance or ownership rule changed. `make test-tracking` now passes all
10 tests; `python3 scripts/bootstrap_tracking.py` completes its offline dry run.
The catalog blocker is resolved; this does not establish integrated acceptance.

`make test-manifest` passed in the network-none container (14 manifest Python
tests, all frontend assertions including the new pilot regressions, and the final
3 claim tests). Scoped health/archive regressions, Python AST, JSON parsing,
Make target dry runs and `git diff --check` passed. Receipt-specific source paths
were compared with their recorded revisions and match; no full Rust build,
full application suite, physical test or final integrated acceptance is implied.

Before Ready/Review: verify SF-082/#160 is actually merged/Done; integrate actual merged
main; rerun compatibility, adversarial, native and end-to-end experiments; require
current-head CI and automatic Testnet validation and independent review. Actual
whole-product multi-device certified encrypted operation/soak at a supported pilot
limit is still unestablished: the separated facet measurements above must not be
combined into an invented integrated throughput or availability claim.

Production provider selection/authentication/fees/quotas/availability/recovery,
manual real-wallet and actual Mainnet acceptance stay unrun in
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
Physical/manual-wallet target acceptance stays in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
These deferrals do not waive source/security, meaningful local/chain regression,
automatic Testnet or independent review gates. Unsupported: production paid-pilot
capacity/SLA, more than measured reader/device/concurrency bounds, long retention
or multi-day soak, provider fleet-scale throttling/recovery, hardware resource use
and firmware safety. No production purchase, credential provisioning or Mainnet
write was performed.
