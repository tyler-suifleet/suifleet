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

## PR #349 feedback repair (2026-10-27)

The new direct `clear_decision_audit` API now takes `&mut TxContext`, matching
customer cleanup and the pinned compiler's ESL06010 rule. The reviewed ABI and
generated TypeScript ABI were recaptured/regenerated; the lint baseline is
unchanged. Three additional Move tests verify fleet-address cleanup authority,
foreign-fleet rejection (`EBinding`) and revoked-admin rejection. Operator docs
now explicitly describe storage growth and cleanup through legacy revision gaps.

Repair checks in `suifleet-dev:sf037` (user UID/GID, `HOME=/home/dev`, worktree
mounted, host Move cache mounted at `/home/dev/.move`):

- `python3 scripts/generate_interfaces.py --capture && make generate-interfaces`:
  passed; only context mutability changed in both ABI files.
- `make test-contracts`: core 297/297 and storage 74/74 passed. Fleet-data was
  independently rerun with `sui move test --path contracts/fleet_data --build-env
  testnet`: 60/60 passed.
- `python3 scripts/check_move_security.py`: passed in full, including pinned
  strict linters, unchanged unsuppressed baselines, package tests and actual
  authority-bypass mutation counterexamples. No diagnostic was added to the baseline.
- `make check-interfaces`: passed; 1098 references, zero drift; generated ABI,
  negative fixtures and all listed client suites passed.
- `node scripts/test_rollout_audit.cjs && node scripts/test_browser_receipt_cleanup.cjs`:
  passed.
- `cd dapp && pnpm typecheck && pnpm build`: passed.
- Fresh compatible upgrade: the equivalent runner above, in a `--network none`
  container with this worktree at its absolute path and common Git directory
  mounted read-only, passed after the signature repair. Existing rollout,
  old-package gaps, pruned-event reads, complete fresh history and deletion all
  passed. All eight effects-checked digests are recorded in
  [repair upgrade evidence](sf164-feedback-upgrade.json).

The protected public-testnet floor-advancement check deliberately fails if the
fullnode's retention floor does not advance within 300 seconds. That endpoint
availability/timing risk remains observable in automatic CI; it is not skipped
or relabeled as a pass. This repair performed no public-chain writes. Hosted
ordinary/security CI and automatic public-testnet acceptance at the repaired
head remain pending for the coordinator, followed by a fresh independent review.
Deferred final acceptance remains #32 and #159 as described above.


## 2026-10-27: retained post-pruning fixture repair

The original public-CI acceptance run failed honestly: its admitted decision was
about 5.5 million checkpoints newer than the fullnode retention floor. A five
minute wait could not demonstrate pruning, and the original `finally` deleted
the rollout, so every retry reset its age. The earlier two orchestration tests
above described that old lifecycle; the repair below supersedes it.

The package-upgrade runner now stores one audit fixture inside its existing
private, authenticated, bytecode-pair/compiler-keyed cache. Its chain, operator,
call package, complete per-type origins, registry and device must match before
reuse. Creation/admission digests and the verified original admission checkpoint
are saved atomically. Each mutation first persists its verified unsigned bytes
and digest, signs only those bytes under the existing strict ledger and a full
20M MIST budget, and reconciles that digest after interruption. Unknown outcomes
are retained without an automatic replay. Outstanding audit gas reservations
are also held back during the next run's funding preflight.

A timeout now retains the exact record and checkpoint. The reader checks the
current record's type/creator, component/version, rollout revision and membership
on each reuse, forbids event-history reads, and still requires the actual node
retention floor to pass the original decision checkpoint before it accepts a
complete current-state audit. Fresh runs check the floor once rather than
sleeping five minutes. Only that explicit floor-pending result has an
`infrastructure` scheduling hint; it remains unsuccessful, never passed or
silently skipped. Other source/reader failures remain failures.

After genuine proof, normal runs retain the small aged record for repeated free
reads. An operator may append `--retire-rollout-audit` to the normal explicitly
configured package-upgrade invocation to reclaim it after proof. Cleanup has the
same durable exact-digest reconciliation. An explicitly retired fixture is not
automatically recreated: do not retire one while its acceptance is still needed.
There is at most one retained rollout per cached bytecode fixture; this repair
does not publish or upgrade package code.

Fresh repair checks:

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p test_rollout_audit_acceptance.py -q`: 18 tests passed, including restart without
  additional transactions, changed provenance/current creator, wrong origin or
  failed effects, missing checkpoint, fake floor/incomplete audit, unknown
  mutation/cleanup, pre-sign persistence and conservative gas reservation.
- Equivalent discovery for `test_testnet_reconcile.py`: 43 tests passed.
- Equivalent discovery for `test_testnet_fixture_cache.py`: 21 tests passed.
- `node scripts/test_rollout_audit.cjs`: passed, including real BCS decoding of
  retained record creator, revision and membership refusals, audit pagination,
  forbidden event fallback and the actual-floor timeout boundary.
- `node --check scripts/read_rollout_audit_live.cjs`, Python AST parsing for the
  changed Python files, and `git diff --check`: passed.

These are fresh offline repair checks, not fresh Move/build or public-chain
acceptance. No wallet was loaded, no faucet/transaction was submitted, and no
hardware or production action was taken during this repair. The previous failed
CI record was deleted by its old cleanup and cannot be resurrected. Automatic
protected Testnet CI must create the new retained fixture once, preserve it
across retries, and obtain actual post-pruning proof after the real floor
advances. Independent source review and exact-head CI remain required; this
repair does not establish that temporal evidence already passed.
