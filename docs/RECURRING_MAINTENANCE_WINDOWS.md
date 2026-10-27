# Recurring maintenance windows: SF-174 decision request

Status: **awaiting product decision. Not approved and not implemented.**
[SF-174/#340](https://github.com/tyler-suifleet/suifleet/issues/340) requires a
recorded product decision on scope, the per-device timezone source and the
on-chain representation before implementation starts. This document lays out the
options and recommends a choice for each. It does not commit to the feature.
Record the decision in #340, then update the status line here. If the feature is
declined, add the reason here and close #340.

Greengrass has no native recurring maintenance window, so this is a product
opportunity, not a parity gap ([parity audit](GREENGRASS_PARITY_AUDIT.md)).

## Current behaviour (SF-018)

`deployment_manager::Control` (dynamic field `ControlKey`) has one absolute
window, `window_start_ms`/`window_end_ms` in UTC Unix milliseconds, plus
`not_before_ms`, `expires_ms` and `notice_ms`. The daemon checks these in
`suifleet-edge/src/control.rs` (`Control::reason`). It checks before preparation
and again at the admission boundary, and it fails closed. Once a window closes, an
operator must edit it; it does not recur
([OPERATIONS.md](OPERATIONS.md#deployment-admission-controls-sf-018)). The chain
does not evaluate windows. Staged-rollout grants (`advance_rollout`) check only
state and expiry, so devices enforce windows locally.

## Decisions requested

### 1. Scope

| Option | Description |
|---|---|
| A (recommended) | Weekly slots: each slot is a day-of-week mask, a local start time (minute of day) and a duration of at most 24 hours. Up to 8 slots per deployment. A recurring window combines with the existing controls by AND: not-before, expiry, pause/cancel, the absolute window (if set) and notice still apply. |
| B | Calendar rules (iCalendar RRULE subset: monthly, Nth weekday, exclusion dates). Much more parser and test surface on-chain and in the daemon. |
| C | Decline. Operators keep editing the single absolute window or scripting edits. |

Out of scope under A: holiday exclusions, per-group or per-device window
overrides, and interrupting an apply already admitted when its window closes. The
SF-018 admission-boundary rule still applies.

### 2. Per-device timezone source

| Option | Description |
|---|---|
| A (recommended) | The deployment carries an IANA zone name (for example `Europe/Berlin`). Every targeted device evaluates the same site-local rule. This is simple, auditable on chain and shown exactly in the editor. Operators use one deployment per timezone. |
| B | Use the device's local configuration (`site_timezone` in `/etc/suifleet/config.toml`), with a deployment sentinel such as `device-local`. This fits fleets that span zones. A missing or invalid value must fail closed ("timezone not configured"), and the editor cannot preview exact instants for every device. |
| C | Store a timezone attribute per device on chain. Adds a device-registry field, owner authority questions and transactions per device. Not recommended. |

A can be extended with B later without a layout change, by reserving the sentinel
value now.

The daemon cannot rely on `/usr/share/zoneinfo`: Yocto images may omit `tzdata`.
The recommendation is to bundle a pinned tz database in the daemon (for example
`jiff` with its bundled tzdb feature, or `chrono-tz`). Record the tzdb version in
`--version` output and in deferral reports. tzdb updates then ship with daemon
updates. The dApp uses the browser's `Intl` zone data. Its preview is advisory
and may differ from the daemon around recent tzdb changes.

### 3. On-chain representation

Recommended: an additive dynamic field with its own introduction-package origin,
matching the SF-018 pattern. Do not change `Control` or `set_control`.

```move
public struct RecurringWindowKey has copy, drop, store {}
public struct WeeklySlot has copy, drop, store {
    days: u8,            // bit 0 = Monday … bit 6 = Sunday; nonzero
    start_minute: u16,   // 0..1439, local wall-clock time
    duration_minutes: u16, // 1..1440
}
public struct RecurringWindow has copy, drop, store {
    revision: u64,
    timezone: String,    // IANA name, ASCII, at most 64 bytes, nonempty
    slots: vector<WeeklySlot>, // 1..8
}
// Creator-only, rejected after cancellation, like set_control.
public fun set_recurring_window(deployment, timezone, days, starts, durations, ctx)
public fun clear_recurring_window(deployment, ctx)
public fun recurring_window(deployment): Option<RecurringWindow>
```

Move validates only the structure: lengths, ranges and ASCII. It cannot validate
zone names; the daemon fails closed on an unknown zone ("unknown timezone"). Any
recurring-window change must also bump the `Control` revision, so the existing
notice restarts and devices re-read admission. Alternatively, the recurring
revision feeds the notice key. The implementation must pick one and test it.

Compatibility: existing records without the field keep current behaviour. Old
daemons ignore the unknown field and would admit outside the recurring window.
Generated manifests must therefore raise the minimum edge version, as SF-018 did
for 0.2.1. The interfaces generator, the deployment manifest type origins and both
client configurations must be regenerated.

## Semantics to fix with the decision

- **Evaluation.** A slot opens at the local date/time `start_minute` on each
  selected day and lasts `duration_minutes` of elapsed time. A slot may cross
  midnight: Sunday 23:00 for 180 minutes ends Monday 02:00. The deferral reason is
  `outside recurring maintenance window`. The local deferral record includes the
  next opening as UTC milliseconds, so the cadence timer can arm for it.
- **DST gap** (the local start does not exist): open at the first valid instant
  after the gap.
- **DST overlap** (the local start occurs twice): open at the earlier occurrence
  only. A window never opens twice in one local day.
- **Clock.** UTC must still be synchronized. Local wall-clock settings and the
  OS `TZ` are not used under option A.
- **Staged rollouts.** Grants may be issued while a device waits for its window.
  Rollout progress and bake timeouts must tolerate this, or operators must size
  `rate_window_ms`/progress expectations to the window. A decision is needed on
  whether `advance_rollout` should skip members known to be outside their window.
  The chain cannot evaluate timezones cheaply, so the recommendation is no:
  document the interaction instead.
- **Editor.** The Deployments schedule editor accepts the zone (searchable IANA
  list, defaulting to the browser zone) and slots. It shows the next three
  admission windows as both local and UTC times, combined with not-before and
  expiry.

## Validation if approved

- Move unit tests: structural validation, creator-only access, rejection after
  cancellation, revision bump, and absent-field default.
- Isolated local-chain upgrade test: an existing record from the previous package,
  with the old entry points still callable.
- Daemon unit tests with fixed tzdb vectors: DST gap/overlap in `Europe/Berlin`
  and `America/New_York`, midnight crossing, a non-DST zone, an unknown zone and
  a malformed slot.
- Simulator (`make test-ota`): deferral outside the window with its reported
  reason, admission inside the window, and an edit that restarts notice.
- `make dapp-typecheck` and `make dapp-build`, with a unit test of the next three
  windows that is consistent with the daemon vectors.
- Applicable automatic testnet CI. Physical-target and real-wallet acceptance stay
  deferred under SF-031/#32 and SF-087/#159.
