# SF-190 storage empty state and query errors

Implementation: Codex, configured/running model gpt-6.1-sol, session
dispatch-8b5928d831354a3581713e3496b89f95, branch
work/suifleet/sf-190-f661c6ee. Issue: https://github.com/tyler-suifleet/suifleet/issues/378.

The fleet-storage query converts the reader's absent result to null, which is
valid TanStack Query data. The hook preserves its public optional storage shape.
The publishing choice explains how to create/fund storage and links to /budget/.
Storage, retention, settings, usage, availability and reclaim scan read errors use
one plain formatter; transport details and query keys are not shown. Mutation and
simulation refusal diagnostics retain their existing behavior.

Verification on 2026-10-27 (all passed):

- Frozen dependency install: corepack pnpm --dir dapp install --frozen-lockfile.
- corepack pnpm --dir dapp typecheck.
- corepack pnpm --dir dapp build: all 16 static routes generated. Existing
  Tailwind module-type warning remains.
- node scripts/test_storage_errors.cjs: actual QueryClient null-success and
  rejection semantics, rendered empty/loading/funded/error storage choice,
  actionable budget link, formatter inputs and rendered usage failure.
- node scripts/test_fleet_budget.cjs.
- node scripts/test_storage_reclaim_panel.cjs.
- node scripts/test_fleet_settings.cjs.
- node scripts/test_storage_reclaim.cjs.
- node scripts/test_budget_enrollment.cjs.
- node scripts/test_fleet_settings_cost.cjs.
- node scripts/test_frontend_queries.cjs.
- git diff --check.
- node scripts/test_storage_errors_testnet.cjs
  /home/tyler/dev/codex/src/suifleet-worktrees/work-suifleet-sf-173-f4e32d34/profiles/seal-testnet
  /tmp/sf190-final-read.json: fresh chain 4c78adac verified, repro budget
  0x1843567958d3267dd9be968780c903a2461e65b21a01fafbc3d0ef9f372058a9
  discovered from BudgetCreated events; actual fetchStorage returns absent under
  package 0x4253e785d19d8835733c744f8e99b2c80730d0c6e854c83d07f5af38e043d374.
  This uses the existing PR #363 public evaluation profile only as read input.
  No writes, wallet, faucet, gas or changes to that profile.

Local host versions: Node v24.21.0, pnpm 12.14.0, TypeScript 5.9.3,
Next 16.4.1; locked dependencies, no dependency changes. The first budget test
attempt exposed its source-slice harness's missing formatter injection; the
harness now supplies the real formatter and checks the plain message. It passed
on rerun.

Limits: this is a read-only Testnet API reproduction plus rendered component
regression, not a manual extension-wallet/browser walkthrough or hosted Site
publication. Final manual real-wallet/provider, hardware and mainnet acceptance
remains unrun in SF-031/#32 and SF-087/#159. No contracts/API/layout changes,
so no package-upgrade or isolated-chain publication is required for this UI fix.
Hosted exact-head CI, automatic Testnet and independent review remain required;
they are pending at implementation handoff, with no approval or merge claimed.

PR #388 feedback repair (2026-10-27):

- Device-retention query now converts an absent optional archive policy to null.
  The regression executes the actual panel query function with the real reader
  and QueryClient, verifying notFound succeeds and transport failure rejects.
- Gas/Walrus quote errors use the shared formatter. Setup/funding links appear
  only for missing budget/storage, not loading, configuration or read failures.
- Source coverage now includes reclaim and component storage views.
- Re-ran frontend typecheck, production build (16 routes), all eight scripts
  listed above and git diff --check: passed. Existing Tailwind warning remains.
- Re-ran the read-only testnet command above with receipt
  /tmp/sf190-feedback-read.json: chain 4c78adac, same absent repro record, zero
  writes/spend. No manual wallet/browser acceptance claimed.
- Fetched origin/main c4c0805c7abd8cfbfbf0d5e0df4a6f285b8da259 and ran
  git merge --no-edit origin/main: already up to date; this base is an ancestor.
  Hosted CI/automatic testnet and fresh independent review remain pending for
  the pushed repair head; the coordinator owns those gates and merging.

PR #388 CodeQL repair (2026-10-27):

- Inspected failed CI run 40614441766, job 122573736883. Its analysis
  completed, then the unchanged security threshold rejected one HIGH finding:
  `js/insecure-temporary-file` in the read-only Testnet repro receipt write.
- Default receipts now live in an atomically created private temporary directory.
  All receipt writes use exclusive creation (`wx`) and mode 0600, refusing an
  existing file or symlink instead of overwriting its target.
- Added `node scripts/test_storage_receipt.cjs` to the normal `test-manifest`
  target. It executes the repro script with offline chain fixtures and real file
  operations, covering private default output, explicit output, existing-file
  refusal and symlink refusal without changing the target. Passed. The same
  regression against the pre-repair script fails because an existing file is
  overwritten rather than rejected.
- Frontend typecheck, production build (16 routes), the eight regression scripts
  above and the new receipt regression passed. Existing Tailwind warning remains.
- Ran the read-only Testnet repro with its default receipt path: chain 4c78adac,
  same absent storage record, zero chain writes/spend. No wallet/lock required.
- Fetched origin/main and merged: already up to date at 0ae332e4. CodeQL CLI is
  unavailable locally; a fresh hosted CodeQL result is required after push.
  No scanner threshold, suppression, dependencies or app behavior changed.
- Hosted CI/automatic Testnet and fresh independent review remain pending.
  Deferred manual wallet/provider/physical/mainnet acceptance stays in #32/#159.
