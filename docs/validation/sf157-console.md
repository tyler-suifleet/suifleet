# SF-157 console presentation validation

Base: `6ae9d2cfb32291c69806dc901c243dd4c5b0fa43` (main). Date: 2026-10-26.

The console uses a shared shell, grouped navigation, consistent page headings,
form controls, cards, tables and focus treatment. Dashboard cards adapt to narrow
screens; enrollment and deployment retain a full page heading while disconnected.
Rollout creation uses a responsive form grid. Data usage follows the Data heading.
Existing route-specific authority, signing and transaction code is unchanged.

History coverage is a secondary **Data status · some history may be missing**
disclosure below page content. Its explanation explicitly warns that lists and
totals can omit older records. Nested technical details preserve the original
checkpoint/cache diagnostic. The underlying warning subscription/discovery logic
is unchanged. No warning is suppressed or relabeled as complete history.

## Checks executed

- `corepack pnpm --dir dapp exec tsc --noEmit`: passed.
- `corepack pnpm --dir dapp build`: passed, all 16 static pages generated.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_dapp_presentation_browser.cjs`:
  passed. Actual production export, all 14 application routes at widths 1440 and
  390; one page heading, one visible active navigation item, no horizontal page
  overflow, no uncaught JavaScript errors. External requests blocked, no wallet.
  Additional real-component fixture verifies populated table/health badge,
  loading, error and disabled states, keyboard-opened history and nested technical
  disclosures, hidden technical text while collapsed, and no footer for empty
  diagnostics. Fixture data is not a live device health result.
- `node scripts/test_incremental_history.cjs`: passed existing cursor, retained
  range, cache corruption/capacity, reload, concurrency and pruning regressions.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_device_history_browser.cjs`:
  passed populated actual device/deployment pages: names, version summary,
  success/failure, device/group search, sorting and stable links. This fixture
  initially failed to bundle because it lacked `useCurrentClient` and
  `useCurrentNetwork` introduced by earlier work. Added those read-only hooks;
  unexpected chain access throws. No network behavior is represented as tested.
- `python3 -m unittest discover -s scripts/tests -p test_tracking.py -q`: 10 passed.
- `git diff --check`: passed.

A separate live development-server visual pass captured all routes at both widths
and inspected dashboard, onboarding and rollout screenshots. Images are local
under `~/.local/state/suifleet/sf157-browser`, not release evidence or committed
user data. The local preview uses the existing public testnet manifest; serving
this UI does not provision newer optional contract features or update the Pi.

## Limits

This is presentation and offline browser validation. No real wallet transaction,
new contract publication, fleet enrollment, on-chain health configuration, device
programming, Mainnet operation or production-provider acceptance was performed.
Disconnected route coverage plus populated fixture checks do not establish every
authenticated page state. Existing missing on-chain health configuration remains
unavailable, rather than showing a fabricated healthy device.

## Plain-language refinement (2026-10-26)

User requested language understandable without blockchain/Sui knowledge. Setup
now explains the wallet as the account approval app, uses funds/network fees
instead of gas, and keeps registration confirmation references in details. Device,
access and deployment copy explains verified reports, permissions, encrypted
software and outcomes. Fee assumptions, exact storage periods and formulas remain
in technical disclosures; SUI/WAL payment units, deposit shortages, missing health,
privacy limitations and destructive key-replacement consequences remain explicit.
No dates are invented from expiry counters. `dapp/AGENTS.md` records this standard
for subsequent work.

Additional actual checks: `node scripts/test_onboarding.cjs`,
`CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_onboarding_browser.cjs`
and `CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_fleet_settings_browser.cjs`
passed. Onboarding covers connection, approval/rejection, duplicate submission,
expiry, unavailable reads and narrow layouts. Settings covers actual constructed
transactions, cost calculations, explicit underfunding confirmation, saved
settings, fleet switching and rejected approval. Tests retain their behavioral
assertions with the new wording and explicitly open the technical quote disclosure.
Frontend typecheck, production build and all-route presentation browser checks
were rerun successfully. All browser wallets/networks are disposable fixtures.
