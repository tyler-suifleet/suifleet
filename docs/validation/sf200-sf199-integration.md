# SF-200 repair against pinned SF-199 main

This is owner conflict repair for PR #450, original source
`7f8fa8799136fcdfe66ef7d08dc9a381a32b3f73`, against the actual conflicting
pinned candidate base `c45c1fc635343b30a390ab466ba76311b3bfa83a`.
It is not a fabricated source-head CI failure or a freshness rebase.

The merge preserves second-person and delay enforcement (availability 13),
stable object-ID intents, chain timestamps for disclosed proposals, pending-index
maintenance, cancellation cleanup, and both independent factor requirements.
The console uses disclosed proposals when SF-199 is present, reads both payload
formats, and supports approved delay-setting edits without mislabeling combined
requirements. Old full-object SF-199 requests need cancellation and requeue.
Policy/Action/Customer layouts and existing type origins remain unchanged.

Local verification uses `suifleet-sf164:protocol140`, compiler 1.81.1 and
transaction CLI 1.83.0. Compiler/local-chain containers use `--network none`,
cached compiler dependencies and disposable identities; no shared wallet writes.

- `sui move test --path contracts`: 393 passed, including combined early,
  missing-approval, mature execution and cancellation/index cleanup cases.
- `sui move test --path contracts/fleet_storage`: 82 passed.
- `sui move test --path contracts/fleet_data --build-env testnet`: 60 passed.
- `make check-interfaces INSIDE_DEV_CONTAINER=1`: passed compiled ABI, generated
  drift, negative ABI, shared vectors and client regressions.
- `node scripts/test_security_delay.cjs`, `node scripts/test_security_policy.cjs`,
  `node scripts/test_second_person.cjs`: passed in the pinned image. Includes
  disclosed queue/cleanup builders, combined settings execution, stable identity,
  tampered identity, disclosed-payload hash checking and bounds.
- `cd dapp && pnpm typecheck && NEXT_TELEMETRY_DISABLED=1 pnpm build`: passed.
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`: 214
  tests OK, one skip (installed image-only toolchain); offline fixtures only.
- `git diff --check`: passed.

Isolated-chain receipts (all signed transaction digests, verified unsigned
package bytes, type origins, execution effects and spending are in the files):

- [Delay existing-object upgrade](sf200-sf199-delay-localnet.json): 16
  transactions, chain `9eb78ebd`, allowance used 1,523,161,196 MIST within
  3,000,000,000; early/direct/old-package/legacy-timestamp refusal, cancellation,
  mature recovery and restored administration passed.
- [Second-person existing-object upgrade](sf200-sf199-approval-localnet.json):
  26 transactions, chain `a3c97b49`, allowance used 1,569,993,399 MIST within
  1,600,000,000; signed net gas 1,531,401,215 MIST. Distinct approval, exact
  execution, old/current bypass refusal, rejection and recovery passed.

- [Exact pinned c45c1fc base upgrade](sf200-c45c1fc-delay-localnet.json):
  16 transactions, chain `541591a1`, allowance used 1,540,448,796 MIST within
  3,000,000,000; compatible upgrade from the actual conflicting SF-199 base,
  old-package direct/timestamp refusal, cancellation and mature recovery passed.

All three runs compiled package digest
`005e7662ee5458b975530871d15f742709f097f8f367ff837a4afe04e22f2db3`.
The first two receipts name the original precommit parent; the pinned-base run
also began before the resolution commit: the conflict
resolution was still uncommitted at execution. Successful actual package upgrades
establish that the combined bytecode fits the chain package-size limit.

Initial local attempts stopped on missing compiler-cache configuration, missing
chain-facts executable, and the container's inaccessible worktree git metadata.
Those setup errors were corrected; they are not successful test evidence.

Hosted CI and automatic public-testnet validation for the pushed repair remain
pending/unverified. No hosted CI polling, new issues, other workers, merge,
production deployment, hardware programming or shared public-wallet mutation
occurred. The coordinator must obtain fresh independent review and enforce the
actual exact-head/base CI/testnet gates. Manual real-wallet, physical Pi,
production-provider and actual-mainnet acceptance remains explicitly unrun in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159); production
hosting remains in [SF-042/#62](https://github.com/tyler-suifleet/suifleet/issues/62).
