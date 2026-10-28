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

## Limits

No manual pass with a real wallet against public testnet was made; the
listed screens were exercised through the browser fixtures and the localnet
harness above. No testnet or mainnet transaction, device programming or
production-provider acceptance was performed (SF-031/#32, SF-087/#159).
