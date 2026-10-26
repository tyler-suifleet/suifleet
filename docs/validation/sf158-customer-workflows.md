# SF-158 customer workflow refinement

Base: main `633bbb5f` (SF-157 merged). 2026-10-26.

## Result and boundaries

Device services explains optional configuration sync, local data buffers,
application messaging and network bridge reports. A missing reporting configuration
is an explicit setup state, not offline health. Private access explains the current
network configuration's missing support, rather than implying the age of a page.
This change does not enable optional contracts, change manifests or update devices.

Settings uses operational choice cards and optional per-option cost comparisons.
Workload inputs, proposed subscription rates and detailed calculations are planning
only. The page explains that saving uses a network transaction, stores the settings
alongside the fleet budget on Sui, and affects devices at their next check. It does
not deposit funds or purchase a subscription. `settingsSaver` and the set transaction
remain unchanged apart from an unsupported-configuration message. Existing
underfunded-save confirmation and fleet/account scope checks remain in place.

Budget separates activity funding (SUI) and storage funding (WAL), personal wallet
funds and fleet balances. Estimation, provider funding and device controls are
optional disclosures. Deposit/swap/storage calculations and transaction builders
are unchanged; asset units and exchange/provider/network fees are retained.
Engineering ticket references and internal document paths are removed from rendered
cost explanations and Greengrass import output; underlying compatibility metadata
and engineering documentation remain intact.

Legacy administration presents named owned resources and three approval steps.
It still calls the existing proposal/confirm/batch/finish APIs. Additional verified
resource identifiers are available under advanced controls. Catalog-linked updates
are excluded from the picker; supported native schemas 1 and 3 remain eligible.
The recipient address and typed list remain bound to the confirmed proposal event
before a transfer reference is shown. Its introduction origin is derived from the
existing Handover type introduced with Proposed, not the latest call package.
Wrong-source/recipient/list events are refused; no guessed reference or retry is
presented as success. The existing transaction hook supplies confirmed events.
Pending submissions have a synchronous guard. Actual authority transfer remains
contract-enforced; this frontend fixture is not live upgrade acceptance. Cancelling
cannot undo completed batches. Credentials, publishing, renewal and other authority
need separate review; a lost administrator key is not recoverable here.

## Checks

- `corepack pnpm --dir dapp exec tsc --noEmit`: passed.
- `corepack pnpm --dir dapp build`: passed; all 16 static pages generated.
- `node scripts/test_fleet_settings_cost.cjs`: passed existing cost, settings,
  manifest-gate and exact built-transaction checks.
- `node scripts/test_fleet_budget.cjs`: passed existing estimates, purchase URL,
  account/reserve, storage transaction and error-path checks.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_fleet_settings_browser.cjs`:
  passed; optional planning starts collapsed, save/payment separation is visible,
  actual save transaction decoded, cost math, funding confirmation, fleet switching,
  rejected approval and unsupported settings retained.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_access_browser.cjs`:
  passed supported/unsupported/missing-provider, loading/error/retry/empty,
  grants/revocation, key-replacement confirmation and pending controls.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_administration_browser.cjs`:
  passed actual named picker and transaction builders, exact recipient/resource IDs
  and kinds, pending guard, confirmed reference, recipient-only confirmation,
  batch transfer/completion and wrong-source event rejection. All chain/approval
  responses are explicit fixtures; no public signing or spending.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_dapp_presentation_browser.cjs`:
  passed all 14 production-export routes at 1440/390px; one heading/active route,
  no page overflow or uncaught errors, and no engineering ticket/internal-doc
  references even in collapsed main-content text. Existing disclosure/state cases
  remain covered; disconnected route coverage is not full authenticated acceptance.
- `python3 -m unittest discover -s scripts/tests -p test_tracking.py -q`: 10 passed.
- `git diff --check`: passed.

Desktop/mobile screenshots of every route are local under
`~/.local/state/suifleet/sf158-browser`; settings, budget and administration were
visually inspected. Local production export preview uses the existing public
testnet configuration at http://localhost:3000. No real wallet/contract/device/
production-provider operation was performed. Required independent review and CI
are separate from these local results.
