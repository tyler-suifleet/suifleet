# SF-170 console polish validation

Base: `88c7079c` (main). Date: 2026-10-27. Implemented by Claude
(claude-opus-5-5).

| Finding | Change |
|---|---|
| Schedule labels "not before ms (UTC)" etc. | "Not before (UTC)", "Expires (UTC)", "Window start (UTC)", "Window end (UTC)"; example placeholders. |
| Environment variable names in device health, alerts and private-upload errors | Plain "not set up; ask your administrator" wording. |
| Blank first column on All deployments | "Update" column removed; the update ID is under each row's Technical details. |
| Stale registration error on Get started | Cleared by a successful account/inventory refresh. |
| New enrollment policy missing from the select | Read from confirmed events, included in the policy query; pending option while loading. |
| Roster Check before groups load | Check and import wait for devices and groups; load failure shows Retry. |
| Created group appears late; duplicates possible | Pending row from the confirmed event; Create disabled until listed (same for device registration/removal). |
| Unconfirmed public upload reaches Review; file input empty on return | Delivery validated on step 2; staged file restored and named. |

## Checks executed

In `suifleet-dev:sf037` (Node/TypeScript) and `suifleet-dev:sf010-chrome`
(headless Chrome), after `pnpm install --frozen-lockfile`:

- `dapp/node_modules/.bin/tsc --noEmit`: passed.
- `dapp/node_modules/.bin/next build`: passed, 16 static pages.
- `node scripts/test_pending_rows.cjs`, `test_alerts.cjs`, `test_fleet_ops.cjs`,
  `test_seal_frontend.cjs`, `test_telemetry.cjs`, `test_enrollment_frontend.cjs`,
  `test_storage_reclaim.cjs`, `test_deployment_control.cjs`, `test_onboarding.cjs`: passed.
- `node scripts/test_dapp_presentation_browser.cjs`: passed. Scans the rendered copy
  and attributes of all 14 exported routes (both widths) and the component
  fixture for `NEXT_PUBLIC_`, `docs/` and `SF-NNN`; asserts the schedule labels.
- `node scripts/test_components_browser.cjs`: passed. Asserts step-2 rejection of
  unconfigured confidential and unconfirmed public delivery without uploads, and
  restored artifact inputs. This fixture failed to bundle on the unmodified base
  (missing `useCurrentNetwork`/`useDAppKit` stubs); stubs added.
- `node scripts/test_onboarding_browser.cjs`: passed, including stale error
  clearing and the pending-registration state with lagged indexing.
- `test_bulk_enrollment_localnet.cjs` on a disposable network-none localnet with
  the debug daemon (`real daemon --enroll`): passed, including policy-select and
  UI group creation (shown at once, one group on chain).

### Review fix: duplicate registration on Devices

Codex review of `58a27c16` found that "Register by address" on Devices was
disabled only while signing, so a confirmed registration not yet indexed could
be submitted again (the contract does not reject a second device for one
address). The button and the form's submit are now disabled while a creation is
pending, and a submission whose address matches an unlisted registration
(including one past the polling timeout) is refused.

- `node scripts/test_devices_pending_browser.cjs` (new, `suifleet-dev:sf010-chrome`):
  passed. Lagged indexing: the pending row shows, Register by address is disabled
  and a forced click neither opens the form nor signs; listing the device
  re-enables it. Fails on `58a27c16` with "pending registration blocks reopening
  Register by address".
- `test_device_history_browser.cjs`, `test_onboarding_browser.cjs`: passed.
- `tsc --noEmit`, `next build`, `test_pending_rows.cjs`, `test_onboarding.cjs`,
  `test_fleet_ops.cjs`, `test_telemetry.cjs` (`suifleet-dev:sf037`): passed.

### Review fix: pending rows lost on tab switch

Codex review of `d3dd3448` found that pending rows lived in the Devices tab's
own state, so Devices → Device groups → Devices dropped a lagged registration
and re-enabled Register by address for the same address. Pending rows are now
kept in a store shared by every screen, keyed by the fleet selection identity
(chain, registry, network, account, fleet) and the list, so they survive tab
and screen changes but never carry into another account or fleet. Get started
and Devices share the device list's rows.

- `node scripts/test_devices_pending_browser.cjs` now switches to Device groups
  and back while the registration is pending and checks that the pending row
  and the disabled button remain and a forced click does not sign. Passed;
  fails on `d3dd3448` (the pending row is gone after returning).
- `tsc --noEmit`, `test_pending_rows.cjs`, `test_onboarding_browser.cjs`,
  `test_components_browser.cjs`, `test_dapp_presentation_browser.cjs`,
  `test_onboarding.cjs`, `next build`, `test_alerts.cjs`, `test_fleet_ops.cjs`,
  `test_seal_frontend.cjs`, `test_telemetry.cjs`, `test_enrollment_frontend.cjs`,
  `test_storage_reclaim.cjs`, `test_deployment_control.cjs`,
  `test_device_history_browser.cjs` (`suifleet-dev:sf010-chrome`): passed.

### Review fix: Get started re-registration after the pending timeout

Codex review of `2a53249b` found that Get started disabled Register device
only while a registration was waiting. After 60 seconds an unlisted
registration becomes stale, the button re-enabled and the same address could
be signed again. The form now refuses an address that matches a waiting or
stale registration before building the transaction, as Devices already does.

- `node scripts/test_onboarding_browser.cjs` now fast-forwards the page clock
  past the timeout, checks the stale notice and the enabled button, submits
  the same address and checks the refusal and that no second signing request
  is made; a refresh after indexing lists the device and clears the refusal.
  Passed; fails on `2a53249b` (no refusal is shown).
- `tsc --noEmit`, `next build`, `test_pending_rows.cjs`, `test_alerts.cjs`,
  `test_fleet_ops.cjs`, `test_seal_frontend.cjs`, `test_telemetry.cjs`,
  `test_enrollment_frontend.cjs`, `test_storage_reclaim.cjs`,
  `test_deployment_control.cjs`, `test_onboarding.cjs`,
  `test_customer_entitlements.cjs`, `test_dashboard_stats.cjs`,
  `test_devices_pending_browser.cjs`, `test_components_browser.cjs`,
  `test_onboarding_browser.cjs`, `test_customer_entitlements_browser.cjs`,
  `test_upload_browser.cjs`, `test_dapp_presentation_browser.cjs`
  (`suifleet-dev:sf010-chrome`): passed.

### Merge of main `24b7752b` (2026-10-28)

CI run 40730581287 on `e844acfb` failed in `check` (`test_testnet_prepared.py`
could not import `yaml`) and in Security CodeQL (actions). Both failures came
from main and also occurred on `a8516400`. PR #361 (SF-179) fixed them, and
this merge brings that fix into the branch. The merge had no conflicts and
changed no SF-170 file.

- `tsc --noEmit`, `next build` and the same 17 node/browser scripts listed
  above (`suifleet-dev:sf010-chrome`): passed on the merged tree.
- `python3 -m unittest discover -s scripts/tests -p test_testnet_prepared.py`
  (host Python with PyYAML): 6 tests passed.

### Merge of main `935e96f8` (2026-10-28)

This merge brings in SF-161 (PR #344), which reads the wallet account through
the hydration-safe `@/hooks/useCurrentAccount`. Two import lines conflicted,
in `enrollment/page.tsx` and `ComponentPublisher.tsx`. They now import the
account from the new hook and keep SF-170's other imports. No other SF-170
code changed.

- `tsc --noEmit`, `next build` and the 17 node/browser scripts listed above,
  plus `test_hydration.cjs` (every exported route, logged out and with a
  reconnected wallet), `test_data_session_browser.cjs`,
  `test_fleet_settings_browser.cjs` and `test_wallet_browser.cjs`
  (`suifleet-dev:sf010-chrome`, `--network none`): all passed on the merged
  tree.
- `python3 -m unittest scripts/tests/test_testnet_prepared.py`: 6 tests passed.

### Merge of main `95ac2b20` (2026-10-28)

This merge brings in the SF-172 landing site (`site/`) and testnet CI
preparation changes. It merged without conflicts and changes nothing under
`dapp/`.

- `tsc --noEmit`, `next build` and the same 21 node/browser scripts as the
  previous merge (`suifleet-dev:sf010-chrome`, `--network none`, isolated
  tmpfs copy): all passed on the merged tree.
- `python3 -m unittest scripts/tests/test_testnet_prepared.py`: 8 tests passed.
- `python3 -m unittest discover -s site/tests`: 18 tests passed.

### Merge of main `b3994cf9` (2026-10-29)

This merge brings in PR #406, which repairs the authenticated testnet
candidate checkout from shallow controllers (the cause of the
`testnet/package-upgrade` error on the previous head). It touches only
`scripts/testnet_ci.py` and its tests, merged without conflicts and changes
nothing under `dapp/`.

- `tsc --noEmit` and `test_pending_rows`, `alerts`, `fleet_ops`,
  `enrollment_frontend`, `deployment_control`, `onboarding`,
  `devices_pending_browser` and `dapp_presentation_browser`
  (`suifleet-dev:sf010-chrome`, `--network none`, isolated tmpfs copy): all
  passed on the merged tree.
- `python3 -m unittest scripts.tests.test_testnet_prepared`: 9 tests passed.

### Merge of main `9d1b68cd` (2026-10-29)

This merge brings in PR #404 (SF-203), which runs the OTA simulator suites
with bounded concurrency. It touches the `test-ota` Makefile target, adds
`scripts/run_ota_suites.py` and its tests, and changes nothing under `dapp/`.
The Makefile merged automatically; this branch's `test_pending_rows` line in
`test-manifest` is kept.

- `tsc --noEmit` and `test_pending_rows`, `alerts`, `fleet_ops`,
  `seal_frontend`, `telemetry`, `enrollment_frontend`, `storage_reclaim`,
  `deployment_control`, `onboarding`, `devices_pending_browser`,
  `components_browser`, `onboarding_browser` and `dapp_presentation_browser`
  (`suifleet-dev:sf010-chrome`, isolated tmpfs copy): all passed on the
  merged tree.
- `python3 -m unittest discover -s scripts/tests -p test_ota_suites.py`:
  6 tests passed.

### Merge of main `0189768b` (2026-10-29)

This merge brings in PR #365 (SF-180), the public site's visual identity,
plus tracking records. It touches only `site/`, `docs/brand/` and
`docs/tracking/`, merged without conflicts and changes nothing under `dapp/`.

- In `suifleet-dev:sf010-chrome` (isolated tmpfs copy,
  `pnpm install --frozen-lockfile`): `tsc --noEmit` and `next build` passed.
  These scripts passed: `test_pending_rows`, `alerts`, `fleet_ops`,
  `seal_frontend`, `telemetry`, `enrollment_frontend`, `storage_reclaim`,
  `deployment_control`, `onboarding`, `devices_pending_browser`,
  `components_browser`, `onboarding_browser`, `upload_browser`,
  `dapp_presentation_browser` and `hydration`.
- `python3 -m unittest discover -s site/tests`: 19 tests passed.

### Merge of main `1a821c11` (2026-10-29)

This merge brings in PR #408 (SF-205), authenticated testnet preparation
errors and bounded repair ownership. It touches only the testnet workflow,
`scripts/testnet_ci.py`, the PR coordinator and their tests and docs. It
merged without conflicts and changes nothing under `dapp/`.

- In `suifleet-dev:sf010-chrome` (isolated tmpfs copy,
  `pnpm install --frozen-lockfile`): `tsc --noEmit` and `next build` passed.
  The same 15 scripts as the previous merge passed.
- `python3 -m unittest scripts.tests.test_testnet`: 45 tests passed.
- `python3 -m unittest discover -s tools/pr-coordinator/tests -p
  test_preparation_failures.py`: 17 tests passed.

### Merge of main `6e45938a` (2026-10-29)

This merge brings in PR #348 (SF-163), the staged rollout builder: the
rollouts page, `RolloutBuilder`, `lib/rollout-form.ts`, their tests and
docs. It merged without conflicts; the only shared file, `Makefile`,
auto-merged and keeps both sides' test steps. No SF-170 file changed.

- `git diff --check origin/main...HEAD` passed.
- In `suifleet-dev:sf010-chrome` (isolated tmpfs copy,
  `pnpm install --frozen-lockfile`): `tsc --noEmit` and `next build` passed.
  The same 15 scripts passed, plus the incoming `rollout_form` and
  `rollout_builder_browser`. The presentation copy scan covers the new
  rollouts screen.
- `python3 -m unittest scripts.tests.test_testnet`: 45 tests passed.

### Merge of main `1bebb345` (2026-10-29)

This merge brings in PR #386 (SF-188), which stops enrollment from an
underfunded fleet budget. Three files conflicted and keep both sides:

- `FirstRun.tsx`: the unlisted-registration refusal and pending rows stay,
  and budget funding is offered only when `canFundEnrollment` holds. The
  checkbox is now main's `BudgetEnrollment`.
- `onboarding-acceptance.jsx`: the lagging-inventory fixture plus main's
  `budgetRace` signing failure.
- `test_onboarding_browser.cjs`: both the refresh-clears-error check and
  the insufficient-budget race check run.

Checks on the merged tree:

- `git diff --check` passed.
- In `suifleet-dev:sf010-chrome` (isolated tmpfs copy,
  `pnpm install --frozen-lockfile`): `tsc --noEmit` and `next build` passed.
  The 17 scripts above passed, plus the incoming `test_fleet_budget`.
- `python3 -m unittest scripts.tests.test_testnet`: 45 tests passed.

### After merging main 48dad86a

This merge brings in PR #396 (SF-197 recovery watchdog and testnet queue
admission). It merged cleanly and touches no SF-170 frontend file.

Checks on the merged tree:

- `git diff --check` passed; main `48dad86a` is an ancestor of the head.
- In `suifleet-dev:sf010-chrome` (isolated tmpfs copy,
  `pnpm install --frozen-lockfile`): `tsc --noEmit` and `next build` passed,
  and the same 18 frontend/browser scripts passed.
- `python3 -m unittest scripts.tests.test_testnet scripts.tests.test_testnet_queue
  scripts.tests.test_testnet_prepared`: 59 tests passed.

### After merging main 00b1076c

This merge brings in PR #410 (SF-206 review-reservation retry in the PR
coordinator). It merged cleanly and touches no SF-170 frontend file.

Checks on the merged tree:

- `git diff --check` passed; main `00b1076c` is an ancestor of the head.
- In `suifleet-dev:sf010-chrome` (isolated tmpfs copy,
  `pnpm install --frozen-lockfile`): `tsc --noEmit` and `next build` passed,
  and the same 18 frontend/browser scripts passed.
- `unshare -rpf --mount-proc python3 -m unittest tests.test_coordinator`
  (in `tools/pr-coordinator`): 58 tests passed.

## Limits

No manual pass with a real wallet against public testnet was made; the
listed screens were exercised through the browser fixtures and the localnet
harness above. No testnet or mainnet transaction, device programming or
production-provider acceptance was performed (SF-031/#32, SF-087/#159).
