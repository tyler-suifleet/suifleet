# SF-164 implementation evidence

2026-10-27, Codex, configured/running model `gpt-6.1-sol`; no finer runtime
build identifier is exposed. Branch `work/suifleet/sf-164-94fd9bdd` in its dispatched
worktree. No dependency tickets. This is implementation evidence, not a release.

## Verification

All commands used the locked frontend dependencies and the pinned
`suifleet-dev:latest` development toolchain (Sui 1.81.1, Node 22.22.2,
pnpm 11.0.8). Containers used `--network none` except dependency installation.
Move compilation/tests used a private temporary empty test wallet configuration;
no normal wallet was read. The upgrade node/wallet were temporary and destroyed.

- `make test-contracts` inside the dev container: 293 core + 74 storage + 60
  fleet-data tests passed. Final core-only rerun after adding the unauthorized
  audit-cleanup negative: `sui move test --path contracts`, 294/294 passed.
- `make check-interfaces`: passed; 1098 client references, zero drift, compiled
  ABI comparison, generated files, ten negative ABI/staleness cases and all
  protocol/catalog/control/rollout/shard/browser-cleanup/inbox checks passed.
- `make dapp-typecheck`: passed, including its frontend/offline prerequisite
  suites. Final `cd dapp && pnpm typecheck && pnpm build`: passed after the
  extracted audit component and bounded deletion planner changes.
- `make dapp-build`: passed; final production build also passed as above.
- `node scripts/test_dashboard_views.cjs` and `node scripts/test_rollout_audit.cjs`:
  passed; full stored pagination with no event reads, old revision gaps, retained
  event recovery, pruning, malformed bindings, pagination/transport refusals,
  exact missing ranges, all audit fields rendered, warning deduplication,
  bounded ordinary/customer cleanup and actual-floor wait/timeout logic.
- `node scripts/test_incremental_history.cjs` and
  `node scripts/test_browser_receipt_cleanup.cjs`: passed.
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`:
  145 tests, OK, one environment-dependent skip on the host (the same suite
  passed all 145 inside the dev-container `make dapp-typecheck` prerequisites).
- `python3 -m unittest discover -s scripts/tests -p 'test_live_common.py'`:
  60/60 passed. Diagnostics about unknown transactions/sweeps are unit fixtures,
  not actual network mutations.
- `python3 -m unittest discover -s scripts/tests -p 'test_deployment_manifest.py'`:
  20/20 passed, including complete/partial later-origin audit groups.
- `python3 -m unittest discover -s scripts/tests -p 'test_rollout_audit_acceptance.py'`:
  2/2 passed (offline orchestration, retained ledger budgets and cleanup on
  success/failure). They do not claim public-network acceptance.
- `make test-rollout-audit-localnet` equivalent runner: passed in disposable
  network-none Docker, with worktree Git metadata mounted read-only for archive.
  Exact command inside container:
  `python3 -c 'import sys; sys.path.insert(0,"scripts"); from test_protocol_localnet import main; main("scripts/test_rollout_audit_upgrade.cjs")'`.
  Real base `0ca16547` publication, existing rollout, compatible head upgrade,
  head decision, still-callable old-package decision, head decision again:
  current-state reader returns revisions 4 and 2, with exact gaps 1 and 3 while
  events are refused as pruned. A fresh rollout has complete stored history;
  both records delete successfully. Package IDs and all eight effects-checked
  transaction digests: [local upgrade evidence](sf164-upgrade.json).
- `git diff --check`: passed.

Initial environment attempts failed before acceptance: an old main-checkout
node_modules tree lacked the pinned SDK verifier; replaced with this worktree's
`pnpm install --frozen-lockfile`. An empty wallet without a build environment
failed configuration selection; a loopback-only testnet build environment fixed
it. The first isolated runner lacked linked-worktree Git metadata; the actual
retry mounted the common Git directory read-only. None is reported as a test pass.

## Limits and next gates

No machine-session Testnet writes, shared resources, hardware or production
credentials were used. The new automatic protected Testnet gate runs in
`live_package_upgrade.py`, using its existing verified fixture and strict ledger.
It reserves 20M MIST per create/admit/delete transaction, independently reads the
last decision's checkpoint, requires the endpoint's reported lowest checkpoint
to advance beyond it within five minutes, and then reads both decisions while
forbidding events. Evidence contains the actual retention floor, decision fields,
digests, effects and existing gas accounting. This hosted run is pending after
push; its success is mandatory, never inferred from the offline fixtures.

Normal exact-head/current-base CI and independent review remain pending for the
coordinator. No live publication or manifest rollout has occurred. After an
actual core upgrade, regenerate manifests to include the three later-origin
`rollout_audit` types. Existing rollouts need no migration and cannot backfill
pruned pre-change events; old-package writes can still create gaps. Explicit
rollout deletion intentionally deletes the audit in bounded chunks.

Manual real-wallet/physical acceptance remains [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32),
and final production/mainnet/provider acceptance remains [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
Those checks are unrun and are not waived or claimed complete.
