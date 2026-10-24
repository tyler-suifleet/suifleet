/// Per-fleet cost settings (SF-130): the cadences that drive a fleet's
/// network cost, chosen by the fleet owner within contract bounds.
///
/// One `Settings` value per fleet lives in a dynamic field of the fleet's
/// `FleetBudget`, so devices find it from the budget ID they already use and
/// a fleet cannot hold two conflicting records. Only the holder of the
/// budget's `BudgetOwnerCap` can write it; every write emits
/// `FleetSettingsChanged` with the previous and new values.
///
/// - `check_interval_ms`: how often devices check the chain for work.
/// - `rollout_check_interval_ms`: faster checks while a device has a
///   deployment awaiting admission (a rollout or scheduled window); 0 is off.
/// - `health_interval_ms`: on-chain health reports (`fleet_data`).
/// - `archive_interval_ms`: routine encrypted log archives (`fleet_data`).
/// - `archive_retention_epochs`: Walrus epochs the owner buys archive storage
///   for (the owner's storage purchases apply it, not devices).
///
/// The bounds keep one device within the public full node's documented
/// guidance of 100 requests per 30 seconds: checks at most every 5 minutes,
/// fast checks at most every 30 seconds and never more often than routine
/// checks. Health and archive bounds equal `fleet_data`'s, whose own
/// configuration remains the enforced minimum spacing of device writes;
/// devices use the slower of the two.
///
/// A fleet without a record keeps each device's local configuration. The
/// owner creates the record with `set` (the dApp starts from `defaults`).
///
/// Introduced by a package upgrade: older package versions cannot reach it.
/// `Settings.version` lets a later upgrade stop older versions overwriting a
/// newer record.
module sui_edge::fleet_settings {
    use sui::clock::Clock;
    use sui::dynamic_field as df;
    use sui::event;
    use sui_edge::fleet_budget::{Self, FleetBudget, BudgetOwnerCap};

    // ── Constants ─────────────────────────────────────────────────────────────

    const VERSION: u64 = 1;

    /// Defaults, from the 2026-10-23 business-plan proposal.
    const DEFAULT_CHECK_INTERVAL_MS: u64 = 1_800_000; // 30 minutes
    const DEFAULT_ROLLOUT_CHECK_INTERVAL_MS: u64 = 60_000; // 60 seconds
    const DEFAULT_HEALTH_INTERVAL_MS: u64 = 86_400_000; // daily
    const DEFAULT_ARCHIVE_INTERVAL_MS: u64 = 604_800_000; // weekly
    const DEFAULT_ARCHIVE_RETENTION_EPOCHS: u32 = 3; // 6 weeks of 2-week Mainnet epochs

    const MIN_CHECK_INTERVAL_MS: u64 = 300_000; // 5 minutes
    const MAX_CHECK_INTERVAL_MS: u64 = 21_600_000; // 6 hours
    const MIN_ROLLOUT_CHECK_INTERVAL_MS: u64 = 30_000;
    const MAX_ROLLOUT_CHECK_INTERVAL_MS: u64 = 600_000; // 10 minutes
    const MIN_HEALTH_INTERVAL_MS: u64 = 60_000;
    const MAX_HEALTH_INTERVAL_MS: u64 = 86_400_000;
    const MIN_ARCHIVE_INTERVAL_MS: u64 = 3_600_000;
    const MAX_ARCHIVE_INTERVAL_MS: u64 = 604_800_000;
    const MIN_ARCHIVE_RETENTION_EPOCHS: u32 = 1;
    /// Walrus's maximum storage purchase ahead.
    const MAX_ARCHIVE_RETENTION_EPOCHS: u32 = 53;

    // ── Errors ────────────────────────────────────────────────────────────────

    const EOutOfBounds: u64 = 0;
    const ENewerVersion: u64 = 1;

    // ── State ─────────────────────────────────────────────────────────────────

    /// Dynamic-field key on the `FleetBudget`.
    public struct SettingsKey has copy, drop, store {}

    public struct Settings has copy, drop, store {
        version: u64,
        check_interval_ms: u64,
        rollout_check_interval_ms: u64,
        health_interval_ms: u64,
        archive_interval_ms: u64,
        archive_retention_epochs: u32,
    }

    /// Every change, with the previous record (none on the first write).
    public struct FleetSettingsChanged has copy, drop {
        budget_id: ID,
        changed_by: address,
        at_ms: u64,
        previous: Option<Settings>,
        settings: Settings,
    }

    // ── Owner ─────────────────────────────────────────────────────────────────

    /// Create or replace the fleet's settings.
    public fun set(
        budget: &mut FleetBudget,
        cap: &BudgetOwnerCap,
        check_interval_ms: u64,
        rollout_check_interval_ms: u64,
        health_interval_ms: u64,
        archive_interval_ms: u64,
        archive_retention_epochs: u32,
        clock: &Clock,
        ctx: &mut TxContext,
    ) {
        fleet_budget::assert_owner(budget, cap);
        let settings = new(check_interval_ms, rollout_check_interval_ms, health_interval_ms,
            archive_interval_ms, archive_retention_epochs);
        let budget_id = object::id(budget);
        let uid = fleet_budget::uid_mut(budget);
        let previous = if (df::exists(uid, SettingsKey {})) {
            let current: &mut Settings = df::borrow_mut(uid, SettingsKey {});
            assert!(current.version <= VERSION, ENewerVersion);
            let previous = *current;
            *current = settings;
            option::some(previous)
        } else {
            df::add(uid, SettingsKey {}, settings);
            option::none()
        };
        event::emit(FleetSettingsChanged {
            budget_id, changed_by: ctx.sender(), at_ms: clock.timestamp_ms(), previous, settings,
        });
    }

    // ── Values ────────────────────────────────────────────────────────────────

    /// A bounds-checked settings value.
    public fun new(
        check_interval_ms: u64,
        rollout_check_interval_ms: u64,
        health_interval_ms: u64,
        archive_interval_ms: u64,
        archive_retention_epochs: u32,
    ): Settings {
        assert!(check_interval_ms >= MIN_CHECK_INTERVAL_MS && check_interval_ms <= MAX_CHECK_INTERVAL_MS, EOutOfBounds);
        assert!(rollout_check_interval_ms == 0 || (rollout_check_interval_ms >= MIN_ROLLOUT_CHECK_INTERVAL_MS
            && rollout_check_interval_ms <= MAX_ROLLOUT_CHECK_INTERVAL_MS
            && rollout_check_interval_ms <= check_interval_ms), EOutOfBounds);
        assert!(health_interval_ms >= MIN_HEALTH_INTERVAL_MS && health_interval_ms <= MAX_HEALTH_INTERVAL_MS, EOutOfBounds);
        assert!(archive_interval_ms >= MIN_ARCHIVE_INTERVAL_MS && archive_interval_ms <= MAX_ARCHIVE_INTERVAL_MS, EOutOfBounds);
        assert!(archive_retention_epochs >= MIN_ARCHIVE_RETENTION_EPOCHS
            && archive_retention_epochs <= MAX_ARCHIVE_RETENTION_EPOCHS, EOutOfBounds);
        Settings {
            version: VERSION, check_interval_ms, rollout_check_interval_ms, health_interval_ms,
            archive_interval_ms, archive_retention_epochs,
        }
    }

    public fun defaults(): Settings {
        new(DEFAULT_CHECK_INTERVAL_MS, DEFAULT_ROLLOUT_CHECK_INTERVAL_MS, DEFAULT_HEALTH_INTERVAL_MS,
            DEFAULT_ARCHIVE_INTERVAL_MS, DEFAULT_ARCHIVE_RETENTION_EPOCHS)
    }

    // ── Reads ─────────────────────────────────────────────────────────────────

    /// The fleet's record; none until the owner first sets it.
    public fun settings(budget: &FleetBudget): Option<Settings> {
        let uid = fleet_budget::uid(budget);
        if (!df::exists(uid, SettingsKey {})) return option::none();
        option::some(*df::borrow(uid, SettingsKey {}))
    }

    public fun version(settings: &Settings): u64 { settings.version }
    public fun check_interval_ms(settings: &Settings): u64 { settings.check_interval_ms }
    public fun rollout_check_interval_ms(settings: &Settings): u64 { settings.rollout_check_interval_ms }
    public fun health_interval_ms(settings: &Settings): u64 { settings.health_interval_ms }
    public fun archive_interval_ms(settings: &Settings): u64 { settings.archive_interval_ms }
    public fun archive_retention_epochs(settings: &Settings): u32 { settings.archive_retention_epochs }

    #[test_only]
    public fun changed_for_testing(budget_id: ID, changed_by: address, at_ms: u64,
        previous: Option<Settings>, settings: Settings): FleetSettingsChanged {
        FleetSettingsChanged { budget_id, changed_by, at_ms, previous, settings }
    }

    #[test_only]
    public fun set_version_for_testing(budget: &mut FleetBudget, version: u64) {
        let current: &mut Settings = df::borrow_mut(fleet_budget::uid_mut(budget), SettingsKey {});
        current.version = version;
    }
}
