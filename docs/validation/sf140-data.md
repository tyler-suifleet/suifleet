# SF-140 Data UI implementation evidence

Owner: Codex, configured/running model `gpt-6.1-sol` (no finer build identifier
available). Branch `work/suifleet/sf-140-303dfa87`; assigned worktree and session
are recorded in SF-140/#285. This is implementation evidence, not production or
manual-wallet acceptance.

## Scope and prerequisite

User-authorized development prerequisite SF139 PR302 was integrated at exact
`b696a61bdbd0769cad1c2b7f86e19bbe73330e13`, preserving upstream history in merge
`2ab6f3d`. Its contract/producer/crypto helper work remains SF139-owned; SF140 changes
only UI, UI data/pagination utilities, tests and documentation/tracking. UI-only
ownership and the immutable helper boundary were coordinated in #284/#285.
SF139 must merge before SF140 integration. Native issue dependencies block all
implementation assertions, so the merge dependency is restored at Review handoff,
after the explicitly authorized pinned-prerequisite development is complete.

The Data section adds bounded generic-item discovery and filters, device/group/
stream details, scoped owner/reader sessions, verified chunk/complete-upload
downloads, small escaped UTF-8/JSON previews, scoped owner grants/revocations and
direct customer HTTPS export. Existing per-device logs/diagnostics/shadow browsing
is preserved. See [dashboard behavior and limits](../DASHBOARD_VIEWS.md#device-data-browser-sf-140).

No production crypto was handwritten: reads use `readDataItem`, `fetchDataItem`
and `scopedDecryptor`; complete uploads pass original verified buffers into
`assembleDataItems`. Grants use `setReadGrant` and the existing effects-checked
wallet hook. Cancellation is checked between chunks and before additional wallet
signing; late buffers are wiped. No helper/contract API changes were needed.

## Verification on the implementation worktree

Toolchain: Node `v22.22.2`, pnpm `11.0.8`, Google Chrome `153.0.8010.36` on the WSL
host. Locked dependencies were installed with `pnpm@11.0.8 install --frozen-lockfile
--dir dapp`. Host commands used the pinned Node/pnpm on PATH; `RUN=` selects host
execution without claiming to be inside the dev image.

- `make dapp-typecheck RUN=`: **passed**, including offline testnet/manifest,
  existing wallet/query/authority/receipt/Seal/archive regressions, the new Data
  unit/browser tests, and `tsc --noEmit`. The 80-test offline testnet suite reports
  one intended host skip: installed-container toolchain verification. Other
  Python groups reported 36, 17, 6 and 3 tests passed. No live testnet run is
  implied by these offline tests.
- `pnpm --dir dapp typecheck`: **passed again** after final cancellation guards.
- `node scripts/test_data_browser.cjs`: **passed again** after final changes.
  Covers bounded one-page-per-action discovery, cursor refusal, device rotation
  and unavailable-device progress; all filters/time errors; preview bounds;
  real signed scoped ciphertext and independent AES-GCM opening; ciphertext
  tamper/access denial; verified assembly/copy refusal; actual scoped grant PTB
  construction; HTTPS/credential/redirect/export errors.
- `node scripts/test_data_browser_browser.cjs`: **passed again** after final
  changes using host Chrome. Exercises the real Data component, filters/details,
  safe JSON preview, actual downloaded bytes, binary refusal of preview, complete
  upload, owner/reader controls, grant/revoke PTBs, multi-item unique export paths,
  destination 403 failure, digest/key denial, scope reset during an in-flight read,
  wiped retained buffers and empty web storage. Playwright fulfills synthetic
  HTTPS requests; no application/fixture server or listening port is started.
- `node scripts/test_fleet_bulk.cjs`: **passed**, preserving existing signed archive
  and live-availability refusal behavior with explicit offline chain fixtures.
- `make dapp-build RUN=`: **passed again** after final cancellation guards; all
  15 static pages generated. Existing Tailwind module-type warning is nonfatal.
- `make test-tracking RUN=`: **10 tests passed**.
- `make tracking-plan RUN=`: **passed**, 119 prepared issues, dry run with no GitHub
  reads/writes, authentication or state mutation.
- `git diff --check`: **passed** before staging.

The first full make attempt mistakenly supplied `INSIDE_DEV_CONTAINER=1` on the
host and failed the installed-toolchain check because `sui` was absent from host
PATH. It was corrected by using the actual host mode (`RUN=`), then the complete
make target passed. No application test was ignored or suppressed. Initial
browser fixture failures (missing fixture process shim, select accessible labels,
and an assertion before React scope cleanup committed) were repaired and rerun.

## Limits and remaining gates

Metadata, sizes and unsalted plaintext hashes remain public; predictable content
can be guessed offline. Legacy archives retain their fleet-wide policy. Revocation
cannot recall plaintext or already obtained keys; epoch rotation denies older
keys including owner reads. The visit holds at most 256 devices/512 generic chunks;
filters cover loaded metadata only. Complete uploads require all chunks loaded;
exports are bounded sets of chunks, not an atomic customer-storage transaction.
URLs/tokens and plaintext stay in tab memory and are discarded on scope changes;
JavaScript/browser copies are not guaranteed erased from memory.

Provider authorization, fullnode/index responses and transaction execution in the
new UI fixtures are explicit stand-ins. Signatures, scoped encryption, integrity
verification, assembly and grant transaction construction are real pinned SDK/
application code. No new live provider, real-wallet, chain or hardware result is
claimed. Historical SF139 evidence is not a current SF140 test run.

At push, hosted ordinary/security CI and automatic public-testnet validation are
**pending**, not approved or waived. The coordinator must enforce actual exact-head/
current-base CI/testnet, fresh independent review and upstream SF139 merged-main
before integration. No CI polling/watchers or model workers were started.
Final manual wallet/IdP/provider, physical target and actual-mainnet acceptance
remains unrun in [#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159). No mainnet, hardware,
production credentials or machine-wallet mutation occurred. No migration is added
by this UI; its deployment must use the updated SF139 package and verified origins.

## PR306 base integration repair (2026-10-25)

The repair snapshot contained no review comments/threads/source findings, but
reported a conflicting branch without its current base. Integrated main
`3c6625c33b238914a2b22d4b7c1a3dd1a69aff27`. The Data page retains upstream
`DataUsage` and the generic browser. Policy entries from both histories remain.
The pinned SF139 producer retains opaque state/anchoring alongside upstream
pre-write authority checks; the empty-log fast path cannot bypass opaque intake.
Upstream publisher fallback regressions supersede the old single-publisher test.

Validation uses host Node 22.22.2, pnpm 11.0.8 and Rust 1.94.0. The initial
frontend target completed its regression scripts but could not find pnpm for its
last tsc command; rerun with the pinned pnpm PATH. The first cargo invocation
lacked a selected toolchain; subsequent invocations explicitly select 1.94.0.
The initial build shell's pnpm wrapper lacked execute permission; corrected before
the successful production build. These setup failures are not test successes.
Hosted CI/testnet and fresh independent review remain pending; SF139 merged-main
is still required. Final external checks remain unrun in #32/#159.

Repair verification results:

- `make dapp-typecheck RUN=` with pinned Node/pnpm PATH: passed, including
  Data unit/Chromium fixtures and final `tsc --noEmit`.
- `make dapp-build RUN=`: passed (16 static pages, existing Tailwind warning).
- `CARGO_BUILD_JOBS=2 CARGO_TARGET_DIR=/home/tyler/.local/state/suifleet/sf139-build cargo +1.94.0 test --locked --manifest-path suifleet-edge/Cargo.toml --features pkcs11`:
  448 passed, 0 failed, 4 ignored; 120.74s test execution.
- Same environment, `cargo +1.94.0 build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example grpc_sim_shim`: passed.
- `python3 -m unittest discover -s scripts/tests -p test_live_common.py`:
  39 passed (offline error/effects/cleanup fixtures, no wallet access).
- `node scripts/test_data_metering.cjs`: passed.
- `python3 scripts/test_fleet_data_ota.py --binary /home/tyler/.local/state/suifleet/sf139-build/debug/suifleet --shim /home/tyler/.local/state/suifleet/sf139-build/debug/examples/grpc_sim_shim --idle-reads`:
  passed: 3 batch reads / 1149 bytes across three idle polls and core-upgrade
  refusal for due writers.
- `docker run --rm --network none --user 0:0 -v "$PWD:/workspace" -v /home/tyler/.local/state/suifleet/sf139-build:/sf140-build:ro -w /workspace suifleet-dev:sf037 python3 scripts/test_opaque_ota.py --binary /sf140-build/debug/suifleet --shim /sf140-build/debug/examples/grpc_sim_shim`:
  passed native signatures/Seal SDK decryption, binary/text/assembly/tamper refusal,
  real IPC custody, certification outage and restart reuse/index retirement.
  The first attempt used a musl runtime image without the host binary's ELF
  loader and failed before starting the fixture; the dev-image rerun passed.
- Conflict paths and SF140 delta whitespace checks passed. The upstream tree has
  a pre-existing blank EOF line in `data-metering.ts`; it was not reformatted.

No full cross-layer suite, new live chain execution, manual wallet/provider,
physical or mainnet acceptance is claimed by this repair. No migrations beyond
upstream SF139 requirements. The coordinator must verify merged SF139 and actual
exact-head/current-base CI/testnet, then obtain fresh independent review.
- Regular `scripts/test_fleet_data_ota.py` with the same freshly built daemon/shim:
  passed signed health/browser verification, health/bulk retries, exhaustion,
  cadence, restart/revocation/diagnostics, encrypted shadow change detection and
  fleet-setting cadence overrides.

## PR306 merged prerequisite integration (2026-10-25)

PR #302 was verified MERGED at `82591d81a130e8eb03f2e176590cf991347aed30`
(mergedAt `2026-10-25T10:59:29Z`) via `gh pr view 302 --json
state,mergedAt,mergeCommit`. The latest fetched main is that commit. Integration
commit `b7577418` contains it. The supplied independent review reported no source
findings; its SF139 prerequisite blocker is now resolved. Producer and publisher
test conflicts use the reviewed merged-main versions exactly; no new producer API
or Data UI behavior is introduced. `git diff origin/main --
suifleet-edge/src/fleet_archive.rs scripts/tests/test_live_common.py` is empty.

Current repair checks:

- `python3 -m unittest discover -s scripts/tests -p test_live_common.py`:
  40 passed; entirely offline mocked helper fixtures, no actual wallet operations.
- `PATH=/tmp/sf140-fix-bin:$PATH make dapp-typecheck RUN=` and
  `make dapp-build RUN=`: passed using host Node 24.21.0 and pnpm 11.0.8.
- `PATH=/tmp/sf140-bin:$PATH pnpm --dir dapp typecheck` and
  `PATH=/tmp/sf140-bin:$PATH make dapp-build RUN=`: passed again using pinned
  Node 22.22.2 / pnpm 11.0.8.
- Under the same pinned PATH, `node scripts/test_data_browser.cjs` and
  `node scripts/test_data_browser_browser.cjs`: passed scoped ciphertext,
  filtering, digest/access refusal, assembly, grants, export, scope cleanup and
  real Chromium UI fixtures. These are software fixtures, not live provider or
  extension-wallet evidence.

Hosted CI and automatic public-testnet acceptance for this new head remain
pending and were not polled. Fresh independent review remains required. Final
manual wallet/provider, physical and actual-mainnet checks remain unrun in
[#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159). No new machine-wallet
mutation, production credentials, hardware operations or merge to main occurred.

- Full `make dapp-typecheck RUN=` repeated with the pinned Node 22.22.2 /
  pnpm 11.0.8 PATH: passed, including offline/frontend regressions.
- `CARGO_BUILD_JOBS=2 CARGO_TARGET_DIR=/home/tyler/.local/state/suifleet/sf139-build cargo +1.94.0 test --locked --manifest-path suifleet-edge/Cargo.toml --features pkcs11`:
  448 passed, 0 failed, 4 ignored; 177.19s.
- Same environment, `cargo +1.94.0 build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example grpc_sim_shim`:
  passed; existing dead-code warnings remain.
- `python3 scripts/test_fleet_data_ota.py --binary /home/tyler/.local/state/suifleet/sf139-build/debug/suifleet --shim /home/tyler/.local/state/suifleet/sf139-build/debug/examples/grpc_sim_shim --idle-reads`:
  passed (3 batch reads / 1152 response bytes over 3 idle polls; upgraded-core
  refusal for due health/archive writers).
- `docker run --rm --network none --user 0:0 -v "$PWD:/workspace" -v /home/tyler/.local/state/suifleet/sf139-build:/sf140-build:ro -w /workspace suifleet-dev:sf037 python3 scripts/test_opaque_ota.py --binary /sf140-build/debug/suifleet --shim /sf140-build/debug/examples/grpc_sim_shim`:
  passed real IPC, durable certification outage/restart/exact ciphertext reuse,
  index retirement and independent pinned SDK signature/Seal/binary/text/assembly/
  tamper refusal. No external network access in this container.
- Regular `python3 scripts/test_fleet_data_ota.py` with the same `--binary`
  and `--shim` paths (without `--idle-reads`): passed signed health/browser
  verification, archive/bulk retries, exhaustion, cadence, restart, revocation,
  diagnostics, encrypted shadow verification and fleet-setting cadence overrides.
- `git diff --check`: passed. Full cross-layer `make check` was not rerun.
