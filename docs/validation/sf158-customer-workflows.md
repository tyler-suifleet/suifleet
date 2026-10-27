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

## PR #323 feedback repair (2026-10-26)

The share-reference card now has separate verified-proposal state. Every submitted
operation clears it, and only a proposal event bound to the requested source,
recipient and ordered resource list sets it. Reviewing another reference or
confirming, transferring, finishing or cancelling cannot supply a share reference.
The browser fixture now checks that the card disappears during and after recipient
acceptance, and that a second proposal with an invalid event cannot reuse the first
proposal's reference, without reloading between proposals.

Repair checks (Docker mount at `/workspace`, network disabled):

- `suifleet-dev:sf037`, working directory `/workspace/dapp`:
  `node_modules/.bin/tsc --noEmit && node_modules/.bin/next build --webpack` passed;
  16 static pages generated.
- `suifleet-dev:sf010-chrome`, working directory `/workspace`:
  `node scripts/test_administration_browser.cjs` passed both new regressions and
  existing selection, pending, exact transaction, accept/batch/finish and invalid
  event tests, with fixture chain and wallet responses only.
- Same browser image: settings-cost and budget unit suites, settings and access
  browser fixtures passed.
- Same browser image: `node scripts/test_dapp_presentation_browser.cjs` passed
  all 14 exported routes at desktop/mobile, with no external network or wallet.
- Host `python3 -m unittest discover -s scripts/tests -p test_tracking.py -q`:
  10 tests passed; `git diff --check` passed.

An initial browser invocation incorrectly specified `/usr/bin/google-chrome`, which
this image does not contain; rerunning with Playwright's bundled browser passed.
An initial corepack invocation attempted registry discovery under disabled network;
the installed compiler and Next.js binaries above passed without network access.
The existing Tailwind module-type warning remains nonfatal. No chain, real wallet,
provider, hardware or deployment actions were performed. Real-wallet/provider and
physical/mainnet acceptance remain unrun in SF-031/#32 and SF-087/#159. Hosted CI
and fresh independent review of the pushed repair remain coordinator merge gates.

## Greengrass guidance feedback repair (2026-10-26)

Customer-facing overrides now explain the unsupported Lambda runtime, native IPC
migration and operator ACL, AWS-managed dependency replacement, and privileged
platform installer boundary without repository paths or planning references. The
shared translator guidance and CLI output remain unchanged. The presentation
browser fixture mounts the actual import component and submits four golden recipe
fixtures, checking the populated rejection results at both 1440px and 390px.
Only storage availability is stubbed; translation and guidance rendering are real,
and uploads/signing are disabled.

Current repair verification (Docker `/workspace` mount, `--network none`):

- `suifleet-dev:sf037`, `/workspace/dapp`: `node_modules/.bin/tsc --noEmit`
  and `node_modules/.bin/next build --webpack` passed; 16 static pages generated.
- `suifleet-dev:sf010-chrome`, `/workspace`: presentation browser suite passed
  all 14 routes and four populated import cases at desktop/mobile widths.
- Same image: `test_greengrass_compat.cjs` passed 83 recipe fixtures and 47
  managed-dependency fixtures; settings-cost and budget suites passed.
- Same image: administration, settings and access browser suites passed.
- Host tracking unit suite: 10 passed; `git diff --check` passed.

The first new browser assertion assumed all dependency reason codes used the
field-specific form; the fixture returned the generic code. The assertion now
accepts either valid code and the rerun passed. The existing nonfatal Tailwind
module-type warning remains. Current main `633bbb5f` was fetched and is already
included in this branch (`git merge --no-edit origin/main`: already up to date).
No migration or chain/wallet/provider/hardware operations were performed. Deferred
external acceptance remains unrun in #32 and #159. Hosted CI and fresh independent
review remain required on the pushed head; this worker does not poll hosted CI.
