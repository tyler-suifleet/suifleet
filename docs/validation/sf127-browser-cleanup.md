# SF-127 browser receipt cleanup source recovery

This manual operator source continuation repairs the wallet cleanup integration
identified after run 40251669356. The eight automatic attempts remain historical
records. No attempt counters, spending ceilings, unknown-outcome reservations or
contract retirement/version/authority checks changed. No public testnet writes,
leftover reconciliation, funding, hardware work or production deployment occurred
in this continuation. Existing public-network leftovers still need ordinary
leased, dedicated-account reconciliation; this evidence does not claim a rebate.

The browser now uses the application's bounded deletion planner and its real
transaction hook for each confirmed step, carrying deployment schema and optional
managed-fleet context. It stops on errors and requires an explicit not-found read
before continuing to group/device cleanup and sweep. The production deployments
page already uses this planner with schema and fleet; no other production caller
of the direct final-delete builder was found.

## Checks

Commands ran against the owner worktree containing main b819cb2010ac4890e4d2d3bb5dad62b9df9b6a8d.
Frontend commands ran in `suifleet-dev:sf035` with `--network none` and the checkout
mounted at `/workspace`; the browser smoke used `suifleet-dev:sf010-chrome`.

- `node scripts/test_browser_receipt_cleanup.cjs`: PASS. Populated shards, multiple
  drain steps, retirement, final deletion, legacy schemas 1/2/3, managed routing,
  explicit absence, transport/permission errors, unsupported schema, failed
  effects and the step bound. Guards actual browser runner wiring.
- `node scripts/test_receipt_shards.cjs`: PASS.
- `node scripts/test_deployment_receipts.cjs`: PASS.
- `node scripts/test_frontend_queries.cjs`: PASS.
- `node scripts/test_customer_fleets.cjs`: PASS.
- `node scripts/live_wallet_browser.cjs --smoke`: PASS, no external requests;
  real bundle exposes the phased cleanup bridge.
- `cd dapp && pnpm typecheck && pnpm build`: PASS.
- `python3 -m unittest discover -s scripts/tests -p 'test_live*.py'`: 51 passed.
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`: 135 tests,
  OK, one skip.
- `node scripts/test_testnet_cache.cjs`: PASS (authenticated roundtrip, wrong key,
  header/nonce/data tampering refusals).

Isolated-chain commands use `suifleet-dev:sf035`, `--network none`, the checkout
mounted at its real absolute path, the common Git directory mounted read-only at
its real path, and `/home/tyler/.move` mounted at `/home/dev/.move`. The compiler
is pinned Sui 1.81.1. All wallet/node state is temporary and loopback-only.

- `python3 scripts/test_protocol_localnet.py --receipt-shards`: PASS; compatible
  current-main upgrade, old-package attacks, migration, 1,000 receipts, old direct
  browser deletion rejected with abort 17, successful phased cleanup of the
  populated deployment/all roots, and successful legacy unsharded cleanup. Maximum
  single-receipt gross storage/computation was 10,985,200 MIST versus the
  60,000,000 MIST test float. Digests are in `sf127-localnet.json`.
- `python3 scripts/test_protocol_localnet.py --receipt-clients`: PASS; actual
  daemon ordinary/catalog v3 reports, rollout health retry, independent indexer,
  old-package deletion rejection and bounded cleanup. Digests are recorded in
  `sf127-clients-localnet.json`.

Initial container setup attempts lacked Git metadata, Move dependency cache or
Chrome and were corrected. The shard regression also exposed stale assertions
for the current base's deletion guard: it now aborts in `delete_as`. Expected
abort code 5 and failed-effects requirements remain unchanged. An initial legacy
fixture with no targets was correctly rejected by the contract; it was corrected
to target an existing test device before the final full passing run.

Fresh independent review and successful exact-head/current-base hosted CI and
automatic testnet acceptance remain required. This is local source-repair
evidence, not merge approval. Hosted checks are not watched by this worker.
Physical and final provider/mainnet/manual-wallet acceptance remain deferred in
SF-031/#32 and SF-087/#159; no such acceptance is claimed here.
