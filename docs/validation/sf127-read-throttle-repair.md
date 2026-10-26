# SF-127 read throttle repair

Run [40266780913](https://github.com/tyler-suifleet/suifleet/actions/runs/40266780913)
reused the verified fixture, passed the four daemon outcome checks and failed
while collecting final independent receipts: `dynamic-field` returned HTTP 429
Too Many Requests after the existing three-read retry bound. Its log records
76,972,249 MIST allowance used and three transactions. This is failed acceptance,
not successful cleanup or a rebate. No public mutation or reconciliation was
performed by this repair worker.

Integrated snapshot/current main `7d6607d451d1469c32a5a9debaf0f9826e29fcfe`,
including its reviewed exact-byte signing and fixture recovery changes. Conflict
resolution preserves the 1.6 SUI package allowance and main's 1.5 SUI security
allowance (3.25 SUI selected-suite preflight including wallet), with the existing
full reservations and non-replenishing accounting. Historical attempts and
quarantines remain intact.

Known read-only commands now make at most five attempts on explicit HTTP 429
Too Many Requests, with 5/10/20/40-second pauses (75 seconds total cooldown).
Persistent throttling still fails. Mutations, unknown commands and other errors
are never replayed. No effects, receipt or cleanup requirement is bypassed.

Local verification after integration and repair:

- `python3 -m unittest discover -s scripts/tests -p 'test_live*.py'`: 51 passed.
  Includes real-response recovery after four throttles, persistent-throttle
  failure and no mutation/non-throttle replay through both CLI adapters.
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`:
  142 tests OK, one installed-toolchain skip on host.
- `python3 -m unittest discover -s scripts/tests -p 'test_release_plan.py'`:
  27 passed.
- Network-none `suifleet-dev:sf035`: browser cleanup, receipt shards,
  deployment receipts, frontend queries, customer fleets and authenticated cache
  Node checks passed; `cd dapp && pnpm typecheck && pnpm build` passed.
- Network-none `suifleet-dev:sf010-chrome` browser smoke passed.
- `git diff --check`: passed.

No contract, daemon or indexer source changed in this repair. Earlier isolated
chain/OTA evidence remains historical and was not rerun here. The longer cooldown
cannot guarantee public provider availability. New-head hosted CI and automatic
testnet remain pending, with fresh independent review required; the worker does
not poll hosted CI or merge. Physical acceptance remains unrun in SF-031/#32;
mainnet/final production-provider/manual-wallet acceptance remains deferred and
unrun in SF-087/#159 and SF-031/#32. Existing public leftovers are not claimed
reconciled.

## Review correction: connect the runtime adapter

Independent review of `9841c915` found that the main merge had restored a direct
`subprocess.run` inside `live_package_upgrade.run()`. The helper tests above
passed, but the runtime path bypassed the read cooldown. The earlier claim that
this repair reached the final receipt reads was therefore incorrect.

The runtime adapter now delegates to `package_client`, which uses the shared
bounded read retry. The extracted helper retains main's failed `ptb` and
`execute-signed-tx` digest readback, including `UnreadableExecution` when effects
cannot be recovered; it never resubmits the mutation. New offline regressions
verify the runtime delegation, failed-effects recovery without replay, and the
unknown-execution failure path. The existing helper regressions exercise four
429s followed by a real dynamic-field response, persistent throttling and
non-retryable commands/errors.

Verification for this correction:

- `python3 -m unittest discover -s scripts/tests -p 'test_live*.py'`: 54 passed.
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`:
  142 tests OK, one installed-toolchain skip on host.
- `python3 -m unittest discover -s scripts/tests -p 'test_release_plan.py'`:
  27 passed.
- `git diff --check`: passed.
- `git merge --no-edit origin/main`: already up to date at `7d6607d4`;
  the snapshot/current base is already an ancestor of this branch.

No frontend, Move, Rust, simulator or live-chain tests were rerun for this Python
adapter correction. Hosted CI and automatic public-testnet acceptance on the
new head remain pending, and fresh independent review is required. The deferred
external checks and unreconciled public leftovers described above remain unrun.
