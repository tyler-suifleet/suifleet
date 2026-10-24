# Per-fleet cost settings (SF-130)

Each customer fleet chooses the cadences that drive its network cost. The
settings live on chain, bounded by the contract, and devices apply them within
one check. The defaults are those of the
[2026-10-23 business-plan proposal](BUSINESS_PLAN.md): the base price includes
them, and faster options are priced add-ons. The settings page that shows the
cost and collects the prepaid deposit is SF-131 (#266).

## Settings, defaults and bounds

| Field | Default | Bounds (inclusive) | Applied by |
|---|---|---|---|
| `check_interval_ms` | 30 minutes | 5 minutes to 6 hours | every device's chain check |
| `rollout_check_interval_ms` | 60 seconds | 30 seconds to 10 minutes, at most `check_interval_ms`; 0 turns fast checks off | the fast checks of the [check cadence](OPERATIONS.md#check-cadence-and-endpoint-fallback-sf-123) |
| `health_interval_ms` | daily | 1 minute to 1 day (as `fleet_data`) | on-chain health reports (`fleet_data`) |
| `archive_interval_ms` | weekly | 1 hour to 1 week (as `fleet_data`) | routine encrypted log archives (`fleet_data`) |
| `archive_retention_epochs` | 3 (six weeks of two-week Mainnet epochs) | 1 to 53 Walrus epochs | the owner's archive storage purchases |

The values are shared through `interfaces/fleet-v1.json` (`fleet_settings`):
the generated `FLEET_SETTINGS` (TypeScript) and `fleet_settings_values` (Rust)
constants, and a generated Move test that checks the contract's defaults and
accepts every bound.

The bounds keep one device within Mysten's documented public full node guidance
of 100 requests per 30 seconds: routine checks at most every 5 minutes and fast
checks at most every 30 seconds. Health and archive bounds equal those
`fleet_data` already enforces.

## Contract: `sui_edge::fleet_settings`

Introduced by a compatible upgrade of the core package. One `Settings` value
per fleet is stored in a dynamic field (key `SettingsKey`) of the fleet's
`FleetBudget` ([FLEET_BUDGET.md](FLEET_BUDGET.md)), so:

- devices find it from the budget ID they already use, with one object read;
- a fleet has one record, never two that disagree;
- budgets created before the upgrade gain a record without migration.

| Function | Who | Effect |
|---|---|---|
| `set(budget, cap, check, rollout, health, archive, retention, clock)` | holder of the budget's `BudgetOwnerCap` | creates or replaces the record; aborts `EOutOfBounds` outside the bounds |
| `defaults()`, `new(...)` | anyone | a bounds-checked value |
| `settings(budget)` and getters | anyone | the record, or none |

Every write emits `FleetSettingsChanged { budget_id, changed_by, at_ms,
previous, settings }`; `previous` is none on the first write. `set` checks the
budget's package version through `fleet_budget::assert_owner`, and refuses to
overwrite a record written by a newer package version (`ENewerVersion`), so an
older package version cannot undo a later migration. There is no device-side
write.

## Daemon

The daemon follows the settings of the budget named by `[budget].budget_id`,
or else `[fleet_data].budget_id`; configuration validation requires both to
name the same budget when both are set. A deployment manifest without the
`fleet_settings` type origins, or a fleet without a record, leaves the local
configuration in force.

- **Checks.** After every check the daemon reads the record and applies it
  to the [check cadence](OPERATIONS.md#check-cadence-and-endpoint-fallback-sf-123)
  as floors: the routine interval becomes
  `max(runtime.poll_seconds, check_interval_ms)` and the fast interval
  (used while a deployment waits for rollout eligibility or admission, or a
  maintenance window is open) becomes
  `max(runtime.rollout_poll_seconds, rollout_check_interval_ms)`. With fast
  checks off (0) the fast interval is the routine interval. The cadence's
  single wakeups at a known admission time or window start, and its failure
  backoff, are unchanged. Intervals that are not whole seconds round up.
- **Health and archives.** The `fleet_data` loop reads the record each round
  and reports health or archives logs at the slower of the `fleet_data`
  cadence (the contract-enforced minimum) and the fleet's setting. With a
  record, that loop also runs at the fleet's check interval instead of every
  minute, so diagnostics requests and archive cleanup follow within one check.
- **Retention** is applied by the owner's storage purchases (pool epochs and
  renewal targets), not by devices; the daemon reads but does not use it.

Local configuration can only make a device slower and cheaper, never faster
than the fleet allows. A change applies at the next check after it is
written: within one interval. A record the daemon cannot use (unreadable,
outside the bounds, or a newer version) keeps the settings already in force
and is logged once. Older daemons ignore the record and keep their local
configuration.

## dApp

`dapp/src/lib/fleet-settings.ts` provides `DEFAULT_SETTINGS`,
`SETTINGS_BOUNDS`, `settingsErrors` (the contract's rules, checked before
signing), `buildSetFleetSettingsTx` and `fetchFleetSettings` (one dynamic-field
read). The `/settings` page (SF-131) prices each setting, sizes the prepaid
deposit from them and saves them with `buildSetFleetSettingsTx`; see
[FLEET_BUDGET.md](FLEET_BUDGET.md).

## Verification

| Command | Covers |
|---|---|
| `sui move test --path contracts fleet_settings` (in `make test-contracts`) | defaults, inclusive bounds, owner-only writes, events with previous values, version gates, additive record on an existing budget |
| `cargo test fleet_settings` (in `make edge-test`) | parsing and bounds, local-only-slower floors on the check cadence, fast checks off, keeping settings in force on unusable reads |
| `python3 scripts/test_fleet_settings_ota.py` (in `make test-ota`) | the real daemon against the simulator: local cadence without a record, refused records, the fast interval during a notice in an open maintenance window, a change picked up within one interval, no checks before the routine interval |
| `scripts/test_fleet_data_ota.py` (in `make test-ota`) | health and routine archives slowed beyond the `fleet_data` minimum |
| `node scripts/test_fleet_settings.cjs` (in `make test-manifest`) | the dApp's defaults, bounds, transaction and read |
| `make test-settings-localnet` (disposable network-none localnet) | base-to-head upgrade over an existing budget, type origins, owner changes and events, on-chain refusals, base-package budget operations, real daemon and dApp reads |

Public-testnet upgrade acceptance runs in the normal automatic testnet CI.
Physical-device and Mainnet acceptance remain in
[#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159).
