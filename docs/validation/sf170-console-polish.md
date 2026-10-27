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

## Limits

No manual pass with a real wallet against public testnet was made; the
listed screens were exercised through the browser fixtures and the localnet
harness above. No testnet or mainnet transaction, device programming or
production-provider acceptance was performed (SF-031/#32, SF-087/#159).
