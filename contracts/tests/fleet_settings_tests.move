#[test_only]
module sui_edge::fleet_settings_tests {
    use sui::clock::{Self, Clock};
    use sui::coin;
    use sui::event;
    use sui::sui::SUI;
    use sui::test_scenario::{Self as ts};
    use sui_edge::device_registry::{Self, DeviceRegistry};
    use sui_edge::fleet_budget::{Self, FleetBudget, BudgetOwnerCap};
    use sui_edge::fleet_settings::{Self, FleetSettingsChanged};

    const OWNER: address = @0xA1;
    const STRANGER: address = @0xBAD;

    const MINUTE: u64 = 60_000;
    const HOUR: u64 = 3_600_000;
    const DAY: u64 = 86_400_000;
    const WEEK: u64 = 604_800_000;

    fun clock_at(scenario: &mut ts::Scenario, now: u64): Clock {
        let mut clock = clock::create_for_testing(ts::ctx(scenario));
        clock.set_for_testing(now);
        clock
    }

    /// Create a budget owned by `owner` (sender of the next transaction).
    fun create_budget(scenario: &mut ts::Scenario, owner: address): ID {
        ts::next_tx(scenario, owner);
        let registry = ts::take_shared<DeviceRegistry>(scenario);
        let deposit = coin::mint_for_testing<SUI>(1_000, ts::ctx(scenario));
        let limits = fleet_budget::new_limits(1, DAY, 1, 1, 1);
        let cap = fleet_budget::create(&registry, deposit, limits, 0, ts::ctx(scenario));
        transfer::public_transfer(cap, owner);
        ts::return_shared(registry);
        ts::next_tx(scenario, owner);
        ts::most_recent_id_shared<FleetBudget>().destroy_some()
    }

    fun setup(): (ts::Scenario, ID) {
        let mut scenario = ts::begin(OWNER);
        device_registry::init_for_testing(ts::ctx(&mut scenario));
        let budget_id = create_budget(&mut scenario, OWNER);
        (scenario, budget_id)
    }

    fun set_as(scenario: &mut ts::Scenario, sender: address, budget_id: ID,
        check: u64, rollout: u64, health: u64, archive: u64, retention: u32) {
        ts::next_tx(scenario, sender);
        let mut budget = ts::take_shared_by_id<FleetBudget>(scenario, budget_id);
        let cap = ts::take_from_address<BudgetOwnerCap>(scenario, sender);
        let clock = clock_at(scenario, 1_000);
        fleet_settings::set(&mut budget, &cap, check, rollout, health, archive, retention, &clock, ts::ctx(scenario));
        clock::destroy_for_testing(clock);
        ts::return_to_address(sender, cap);
        ts::return_shared(budget);
    }

    fun set(scenario: &mut ts::Scenario, budget_id: ID, check: u64, rollout: u64, health: u64, archive: u64, retention: u32) {
        set_as(scenario, OWNER, budget_id, check, rollout, health, archive, retention)
    }

    #[test]
    fun defaults_match_the_business_plan() {
        let d = fleet_settings::defaults();
        assert!(fleet_settings::version(&d) == 1, 0);
        assert!(fleet_settings::check_interval_ms(&d) == 30 * MINUTE, 1);
        assert!(fleet_settings::rollout_check_interval_ms(&d) == MINUTE, 2);
        assert!(fleet_settings::health_interval_ms(&d) == DAY, 3);
        assert!(fleet_settings::archive_interval_ms(&d) == WEEK, 4);
        assert!(fleet_settings::archive_retention_epochs(&d) == 3, 5);
    }

    #[test]
    fun owner_sets_and_changes_settings_with_auditable_events() {
        let (mut scenario, budget_id) = setup();
        ts::next_tx(&mut scenario, OWNER);
        {
            let budget = ts::take_shared_by_id<FleetBudget>(&scenario, budget_id);
            assert!(fleet_settings::settings(&budget).is_none(), 0);
            ts::return_shared(budget);
        };
        set(&mut scenario, budget_id, 30 * MINUTE, MINUTE, DAY, WEEK, 3);
        let first = ts::next_tx(&mut scenario, OWNER);
        assert!(first.num_user_events() == 1, 1);
        set(&mut scenario, budget_id, 15 * MINUTE, 0, 6 * HOUR, DAY, 13);
        let events = event::events_by_type<FleetSettingsChanged>();
        assert!(events.length() == 1, 2);
        ts::next_tx(&mut scenario, OWNER);
        {
            let budget = ts::take_shared_by_id<FleetBudget>(&scenario, budget_id);
            let s = fleet_settings::settings(&budget).destroy_some();
            assert!(fleet_settings::check_interval_ms(&s) == 15 * MINUTE, 3);
            assert!(fleet_settings::rollout_check_interval_ms(&s) == 0, 4);
            assert!(fleet_settings::health_interval_ms(&s) == 6 * HOUR, 5);
            assert!(fleet_settings::archive_interval_ms(&s) == DAY, 6);
            assert!(fleet_settings::archive_retention_epochs(&s) == 13, 7);
            ts::return_shared(budget);
        };
        ts::end(scenario);
    }

    #[test]
    fun events_carry_the_previous_and_new_values() {
        let (mut scenario, budget_id) = setup();
        set(&mut scenario, budget_id, 30 * MINUTE, MINUTE, DAY, WEEK, 3);
        let events = event::events_by_type<FleetSettingsChanged>();
        assert!(events.length() == 1, 0);
        let expected = fleet_settings::defaults();
        assert!(events[0] == fleet_settings::changed_for_testing(budget_id, OWNER, 1_000, option::none(), expected), 1);
        set(&mut scenario, budget_id, HOUR, 0, DAY, WEEK, 3);
        let events = event::events_by_type<FleetSettingsChanged>();
        let next = fleet_settings::new(HOUR, 0, DAY, WEEK, 3);
        assert!(events[0] == fleet_settings::changed_for_testing(budget_id, OWNER, 1_000, option::some(expected), next), 2);
        ts::end(scenario);
    }

    #[test]
    fun bounds_are_inclusive() {
        let (mut scenario, budget_id) = setup();
        set(&mut scenario, budget_id, 5 * MINUTE, 30_000, MINUTE, HOUR, 1);
        set(&mut scenario, budget_id, 6 * HOUR, 10 * MINUTE, DAY, WEEK, 53);
        // A fast check equal to the routine check is allowed.
        set(&mut scenario, budget_id, 5 * MINUTE, 5 * MINUTE, DAY, WEEK, 3);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ENotOwner)]
    fun another_fleets_cap_cannot_change_settings() {
        let (mut scenario, budget_id) = setup();
        create_budget(&mut scenario, STRANGER);
        set_as(&mut scenario, STRANGER, budget_id, 30 * MINUTE, MINUTE, DAY, WEEK, 3);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EWrongVersion)]
    fun an_older_package_cannot_write_a_migrated_budget() {
        let (mut scenario, budget_id) = setup();
        ts::next_tx(&mut scenario, OWNER);
        {
            let mut budget = ts::take_shared_by_id<FleetBudget>(&scenario, budget_id);
            fleet_budget::set_version_for_testing(&mut budget, 2);
            ts::return_shared(budget);
        };
        set(&mut scenario, budget_id, 30 * MINUTE, MINUTE, DAY, WEEK, 3);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_settings::ENewerVersion)]
    fun an_older_package_cannot_overwrite_a_newer_record() {
        let (mut scenario, budget_id) = setup();
        set(&mut scenario, budget_id, 30 * MINUTE, MINUTE, DAY, WEEK, 3);
        ts::next_tx(&mut scenario, OWNER);
        {
            let mut budget = ts::take_shared_by_id<FleetBudget>(&scenario, budget_id);
            fleet_settings::set_version_for_testing(&mut budget, 2);
            ts::return_shared(budget);
        };
        set(&mut scenario, budget_id, HOUR, MINUTE, DAY, WEEK, 3);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_settings::EOutOfBounds)]
    fun check_interval_below_five_minutes_is_rejected() {
        fleet_settings::new(5 * MINUTE - 1, 0, DAY, WEEK, 3);
    }

    #[test, expected_failure(abort_code = fleet_settings::EOutOfBounds)]
    fun check_interval_above_six_hours_is_rejected() {
        fleet_settings::new(6 * HOUR + 1, 0, DAY, WEEK, 3);
    }

    #[test, expected_failure(abort_code = fleet_settings::EOutOfBounds)]
    fun fast_check_below_thirty_seconds_is_rejected() {
        fleet_settings::new(30 * MINUTE, 29_999, DAY, WEEK, 3);
    }

    #[test, expected_failure(abort_code = fleet_settings::EOutOfBounds)]
    fun fast_check_above_ten_minutes_is_rejected() {
        fleet_settings::new(HOUR, 10 * MINUTE + 1, DAY, WEEK, 3);
    }

    #[test, expected_failure(abort_code = fleet_settings::EOutOfBounds)]
    fun fast_check_slower_than_routine_check_is_rejected() {
        fleet_settings::new(5 * MINUTE, 5 * MINUTE + 1, DAY, WEEK, 3);
    }

    #[test, expected_failure(abort_code = fleet_settings::EOutOfBounds)]
    fun health_below_one_minute_is_rejected() {
        fleet_settings::new(30 * MINUTE, MINUTE, MINUTE - 1, WEEK, 3);
    }

    #[test, expected_failure(abort_code = fleet_settings::EOutOfBounds)]
    fun health_above_one_day_is_rejected() {
        fleet_settings::new(30 * MINUTE, MINUTE, DAY + 1, WEEK, 3);
    }

    #[test, expected_failure(abort_code = fleet_settings::EOutOfBounds)]
    fun archive_below_one_hour_is_rejected() {
        fleet_settings::new(30 * MINUTE, MINUTE, DAY, HOUR - 1, 3);
    }

    #[test, expected_failure(abort_code = fleet_settings::EOutOfBounds)]
    fun archive_above_one_week_is_rejected() {
        fleet_settings::new(30 * MINUTE, MINUTE, DAY, WEEK + 1, 3);
    }

    #[test, expected_failure(abort_code = fleet_settings::EOutOfBounds)]
    fun zero_retention_is_rejected() {
        fleet_settings::new(30 * MINUTE, MINUTE, DAY, WEEK, 0);
    }

    #[test, expected_failure(abort_code = fleet_settings::EOutOfBounds)]
    fun retention_beyond_walrus_maximum_is_rejected() {
        fleet_settings::new(30 * MINUTE, MINUTE, DAY, WEEK, 54);
    }

    #[test, expected_failure(abort_code = fleet_settings::EOutOfBounds)]
    fun an_out_of_bounds_write_leaves_the_record_unchanged() {
        let (mut scenario, budget_id) = setup();
        set(&mut scenario, budget_id, 30 * MINUTE, MINUTE, DAY, WEEK, 3);
        set(&mut scenario, budget_id, MINUTE, MINUTE, DAY, WEEK, 3);
        ts::end(scenario);
    }

    /// A budget created before this module existed gains settings without
    /// disturbing its device index or totals.
    #[test]
    fun settings_are_additive_on_an_existing_budget() {
        let (mut scenario, budget_id) = setup();
        ts::next_tx(&mut scenario, OWNER);
        let (count, balance) = {
            let budget = ts::take_shared_by_id<FleetBudget>(&scenario, budget_id);
            let count = fleet_budget::device_count(&budget);
            let balance = fleet_budget::balance(&budget);
            ts::return_shared(budget);
            (count, balance)
        };
        set(&mut scenario, budget_id, 30 * MINUTE, MINUTE, DAY, WEEK, 3);
        ts::next_tx(&mut scenario, OWNER);
        {
            let mut budget = ts::take_shared_by_id<FleetBudget>(&scenario, budget_id);
            assert!(fleet_budget::device_count(&budget) == count && fleet_budget::balance(&budget) == balance, 0);
            // Owner operations still work alongside the record.
            let cap = ts::take_from_address<BudgetOwnerCap>(&scenario, OWNER);
            fleet_budget::deposit(&mut budget, &cap, coin::mint_for_testing<SUI>(5, ts::ctx(&mut scenario)));
            assert!(fleet_budget::balance(&budget) == balance + 5, 1);
            assert!(fleet_settings::settings(&budget).is_some(), 2);
            ts::return_to_address(OWNER, cap);
            ts::return_shared(budget);
        };
        ts::end(scenario);
    }
}
