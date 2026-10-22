#[test_only]
module sui_edge::fleet_budget_tests {
    use std::string;
    use sui::clock::{Self, Clock};
    use sui::coin::{Self, Coin};
    use sui::event;
    use sui::sui::SUI;
    use sui::test_scenario::{Self as ts};
    use sui_edge::device_cap::{Self, DeviceCap};
    use sui_edge::device_registry::{Self, DeviceRegistry, DeviceOwnerCap};
    use sui_edge::fleet_budget::{Self, FleetBudget, BudgetOwnerCap, GasDrawn, BudgetLow, DeviceFunded};

    const OWNER: address = @0xA1;
    const DEVICE: address = @0xD1;
    const DEVICE_2: address = @0xD2;
    const NEW_KEY: address = @0xD9;
    const STRANGER: address = @0xBAD;

    const FLOAT: u64 = 50_000_000;
    const DAY: u64 = 86_400_000;
    const ALLOWANCE: u64 = 100_000_000;
    const MAX_DRAWS: u64 = 4;
    const INTERVAL: u64 = 60_000;
    const DEPOSIT: u64 = 10_000_000_000;

    fun limits(): fleet_budget::Limits {
        fleet_budget::new_limits(FLOAT, DAY, ALLOWANCE, MAX_DRAWS, INTERVAL)
    }

    fun clock_at(scenario: &mut ts::Scenario, now: u64): Clock {
        let mut clock = clock::create_for_testing(ts::ctx(scenario));
        clock.set_for_testing(now);
        clock
    }

    /// Registry, a budget with DEPOSIT and the owner's cap.
    fun setup(): ts::Scenario {
        let mut scenario = ts::begin(OWNER);
        device_registry::init_for_testing(ts::ctx(&mut scenario));
        ts::next_tx(&mut scenario, OWNER);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let deposit = coin::mint_for_testing<SUI>(DEPOSIT, ts::ctx(&mut scenario));
        let cap = fleet_budget::create(&registry, deposit, limits(), 1_000_000_000, ts::ctx(&mut scenario));
        transfer::public_transfer(cap, OWNER);
        ts::return_shared(registry);
        scenario
    }

    /// Register and activate a device keyed by `device`; its cap goes to it.
    fun activate(scenario: &mut ts::Scenario, device: address): ID {
        ts::next_tx(scenario, OWNER);
        let mut registry = ts::take_shared<DeviceRegistry>(scenario);
        let clock = clock_at(scenario, 1);
        let d = device_registry::register_device(&mut registry, string::utf8(b"pi"), device, string::utf8(b"aarch64"), &clock, ts::ctx(scenario));
        let cap = device_cap::issue(&d, ts::ctx(scenario));
        transfer::public_transfer(cap, device);
        let owner_cap = device_registry::activate_device(&mut registry, d, ts::ctx(scenario));
        let device_id = device_registry::owner_cap_device_id(&owner_cap);
        transfer::public_transfer(owner_cap, OWNER);
        clock::destroy_for_testing(clock);
        ts::return_shared(registry);
        device_id
    }

    fun enroll(scenario: &mut ts::Scenario, float: u64) {
        ts::next_tx(scenario, OWNER);
        let mut budget = ts::take_shared<FleetBudget>(scenario);
        let cap = ts::take_from_address<BudgetOwnerCap>(scenario, OWNER);
        let registry = ts::take_shared<DeviceRegistry>(scenario);
        let owner_cap = ts::take_from_address<DeviceOwnerCap>(scenario, OWNER);
        let clock = clock_at(scenario, 1);
        fleet_budget::enroll_device(&mut budget, &cap, &registry, &owner_cap, float, &clock, ts::ctx(scenario));
        clock::destroy_for_testing(clock);
        ts::return_to_address(OWNER, owner_cap);
        ts::return_shared(registry);
        ts::return_to_address(OWNER, cap);
        ts::return_shared(budget);
    }

    /// One device, activated and enrolled with a full float.
    fun enrolled(): (ts::Scenario, ID) {
        let mut scenario = setup();
        let device_id = activate(&mut scenario, DEVICE);
        enroll(&mut scenario, FLOAT);
        (scenario, device_id)
    }

    /// `sender` draws `amount` with `holder`'s DeviceCap at time `now`.
    fun draw_as(scenario: &mut ts::Scenario, sender: address, holder: address, amount: u64, now: u64) {
        ts::next_tx(scenario, sender);
        let mut budget = ts::take_shared<FleetBudget>(scenario);
        let registry = ts::take_shared<DeviceRegistry>(scenario);
        let cap = ts::take_from_address<DeviceCap>(scenario, holder);
        let clock = clock_at(scenario, now);
        fleet_budget::draw_gas(&mut budget, &registry, &cap, amount, &clock, ts::ctx(scenario));
        clock::destroy_for_testing(clock);
        ts::return_to_address(holder, cap);
        ts::return_shared(registry);
        ts::return_shared(budget);
    }

    fun draw(scenario: &mut ts::Scenario, amount: u64, now: u64) {
        draw_as(scenario, DEVICE, DEVICE, amount, now)
    }

    fun balance(scenario: &mut ts::Scenario): u64 {
        ts::next_tx(scenario, OWNER);
        let budget = ts::take_shared<FleetBudget>(scenario);
        let value = fleet_budget::balance(&budget);
        ts::return_shared(budget);
        value
    }

    /// Total SUI the address holds in coins.
    fun held(scenario: &mut ts::Scenario, owner: address): u64 {
        ts::next_tx(scenario, owner);
        let ids = ts::ids_for_address<Coin<SUI>>(owner);
        let mut total = 0;
        let mut i = 0;
        while (i < ids.length()) {
            let coin = ts::take_from_address_by_id<Coin<SUI>>(scenario, owner, ids[i]);
            total = total + coin.value();
            ts::return_to_address(owner, coin);
            i = i + 1;
        };
        total
    }

    fun owner_begin(scenario: &mut ts::Scenario): (FleetBudget, BudgetOwnerCap, DeviceRegistry) {
        ts::next_tx(scenario, OWNER);
        (ts::take_shared<FleetBudget>(scenario), ts::take_from_address<BudgetOwnerCap>(scenario, OWNER), ts::take_shared<DeviceRegistry>(scenario))
    }

    fun owner_end(budget: FleetBudget, cap: BudgetOwnerCap, registry: DeviceRegistry) {
        ts::return_shared(registry);
        ts::return_to_address(OWNER, cap);
        ts::return_shared(budget);
    }

    fun revoke(scenario: &mut ts::Scenario) {
        let (budget, cap, mut registry) = owner_begin(scenario);
        let owner_cap = ts::take_from_address<DeviceOwnerCap>(scenario, OWNER);
        device_registry::revoke_device(&mut registry, &owner_cap);
        ts::return_to_address(OWNER, owner_cap);
        owner_end(budget, cap, registry);
    }

    // ── Ownership and funding ─────────────────────────────────────────────────

    #[test]
    fun enrollment_pays_the_float_to_the_device_address() {
        let (mut scenario, device_id) = enrolled();
        assert!(held(&mut scenario, DEVICE) == FLOAT, 0);
        assert!(balance(&mut scenario) == DEPOSIT - FLOAT, 1);
        ts::next_tx(&mut scenario, OWNER);
        let budget = ts::take_shared<FleetBudget>(&scenario);
        let account = fleet_budget::device_account(&budget, device_id).destroy_some();
        assert!(fleet_budget::account_total_drawn(&account) == FLOAT && fleet_budget::account_drawn_in_period(&account) == 0, 2);
        assert!(fleet_budget::device_count(&budget) == 1 && fleet_budget::total_drawn(&budget) == FLOAT, 3);
        assert!(fleet_budget::spendable(&budget) == DEPOSIT - FLOAT && fleet_budget::reserved(&budget) == 0, 4);
        assert!(fleet_budget::interface_version() == 1 && fleet_budget::version(&budget) == fleet_budget::package_version(), 5);
        ts::return_shared(budget);
        ts::end(scenario);
    }

    #[test]
    fun owner_deposits_and_withdraws_unused_funds() {
        let mut scenario = setup();
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        fleet_budget::deposit(&mut budget, &cap, coin::mint_for_testing<SUI>(5, ts::ctx(&mut scenario)));
        let coin = fleet_budget::withdraw(&mut budget, &cap, DEPOSIT, ts::ctx(&mut scenario));
        assert!(coin.value() == DEPOSIT, 0);
        transfer::public_transfer(coin, OWNER);
        assert!(fleet_budget::balance(&budget) == 5 && fleet_budget::total_withdrawn(&budget) == DEPOSIT, 1);
        assert!(fleet_budget::total_deposited(&budget) == DEPOSIT + 5, 2);
        // Falling below the threshold is reported.
        assert!(event::events_by_type<BudgetLow>().length() == 1, 3);
        let _ = &mut registry;
        owner_end(budget, cap, registry);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EOverFloat)]
    fun enrollment_float_is_capped() {
        let mut scenario = setup();
        activate(&mut scenario, DEVICE);
        enroll(&mut scenario, FLOAT + 1);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EAlreadyEnrolled)]
    fun a_device_enrolls_once() {
        let (mut scenario, _) = enrolled();
        enroll(&mut scenario, 0);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EInactiveDevice)]
    fun a_revoked_device_cannot_be_enrolled() {
        let mut scenario = setup();
        activate(&mut scenario, DEVICE);
        revoke(&mut scenario);
        enroll(&mut scenario, FLOAT);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ENotOwner)]
    fun another_budgets_cap_cannot_withdraw() {
        let mut scenario = setup();
        ts::next_tx(&mut scenario, STRANGER);
        let first = ts::most_recent_id_shared<FleetBudget>().destroy_some();
        // A second budget: its cap must not manage the first.
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let other = fleet_budget::create(&registry, coin::zero<SUI>(ts::ctx(&mut scenario)), limits(), 0, ts::ctx(&mut scenario));
        ts::return_shared(registry);
        ts::next_tx(&mut scenario, STRANGER);
        let mut budget = ts::take_shared_by_id<FleetBudget>(&scenario, first);
        transfer::public_transfer(fleet_budget::withdraw(&mut budget, &other, 1, ts::ctx(&mut scenario)), STRANGER);
        ts::return_shared(budget);
        transfer::public_transfer(other, STRANGER);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EInvalidLimits)]
    fun zero_period_limits_are_rejected() {
        fleet_budget::new_limits(FLOAT, 0, ALLOWANCE, MAX_DRAWS, INTERVAL);
    }

    // ── Draw limits ───────────────────────────────────────────────────────────

    #[test]
    fun a_device_draws_within_its_limits_and_every_draw_is_an_event() {
        let (mut scenario, device_id) = enrolled();
        draw(&mut scenario, 30_000_000, 1_000);
        let effects = ts::next_tx(&mut scenario, OWNER);
        assert!(ts::num_user_events(&effects) == 1, 0);
        draw(&mut scenario, 30_000_000, 1_000 + INTERVAL);
        assert!(held(&mut scenario, DEVICE) == FLOAT + 60_000_000, 1);
        ts::next_tx(&mut scenario, OWNER);
        let budget = ts::take_shared<FleetBudget>(&scenario);
        let account = fleet_budget::device_account(&budget, device_id).destroy_some();
        assert!(fleet_budget::account_drawn_in_period(&account) == 60_000_000, 2);
        assert!(fleet_budget::account_draws_in_period(&account) == 2, 3);
        assert!(fleet_budget::balance(&budget) == DEPOSIT - FLOAT - 60_000_000, 4);
        ts::return_shared(budget);
        ts::end(scenario);
    }

    #[test]
    fun draw_event_is_emitted_in_the_drawing_transaction() {
        let (mut scenario, _) = enrolled();
        ts::next_tx(&mut scenario, DEVICE);
        let mut budget = ts::take_shared<FleetBudget>(&scenario);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let cap = ts::take_from_address<DeviceCap>(&scenario, DEVICE);
        let clock = clock_at(&mut scenario, 5);
        fleet_budget::draw_gas(&mut budget, &registry, &cap, 1, &clock, ts::ctx(&mut scenario));
        assert!(event::events_by_type<GasDrawn>().length() == 1, 0);
        assert!(event::events_by_type<DeviceFunded>().length() == 0, 1);
        clock::destroy_for_testing(clock);
        ts::return_to_address(DEVICE, cap);
        ts::return_shared(registry);
        ts::return_shared(budget);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EOverFloat)]
    fun a_single_draw_cannot_exceed_the_float_cap() {
        let (mut scenario, _) = enrolled();
        draw(&mut scenario, FLOAT + 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EOverAllowance)]
    fun draws_cannot_exceed_the_period_allowance() {
        let (mut scenario, _) = enrolled();
        draw(&mut scenario, FLOAT, 1_000);
        draw(&mut scenario, FLOAT, 1_000 + INTERVAL);
        draw(&mut scenario, 1, 1_000 + 2 * INTERVAL);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ERateLimited)]
    fun draws_are_spaced_by_the_minimum_interval() {
        let (mut scenario, _) = enrolled();
        draw(&mut scenario, 1, 1_000);
        draw(&mut scenario, 1, 1_000 + INTERVAL - 1);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ERateLimited)]
    fun draws_per_period_are_limited() {
        let (mut scenario, _) = enrolled();
        let mut i = 0;
        while (i <= MAX_DRAWS) {
            draw(&mut scenario, 1, 1_000 + i * INTERVAL);
            i = i + 1;
        };
        ts::end(scenario);
    }

    #[test]
    fun a_new_period_restores_the_allowance() {
        let (mut scenario, _) = enrolled();
        draw(&mut scenario, FLOAT, 1_000);
        draw(&mut scenario, FLOAT, 1_000 + INTERVAL);
        // Period starts at enrollment (t=1).
        draw(&mut scenario, FLOAT, 1 + DAY);
        draw(&mut scenario, FLOAT, 1 + DAY + INTERVAL);
        assert!(held(&mut scenario, DEVICE) == FLOAT + 2 * ALLOWANCE, 0);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EInsufficientBalance)]
    fun draws_cannot_exceed_the_budget() {
        let mut scenario = setup();
        activate(&mut scenario, DEVICE);
        enroll(&mut scenario, 0);
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        let all = fleet_budget::balance(&budget);
        transfer::public_transfer(fleet_budget::withdraw(&mut budget, &cap, all - 10, ts::ctx(&mut scenario)), OWNER);
        let _ = &mut registry;
        owner_end(budget, cap, registry);
        draw(&mut scenario, 11, 1_000);
        ts::end(scenario);
    }

    #[test]
    fun owner_limits_apply_per_device() {
        let (mut scenario, device_id) = enrolled();
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        fleet_budget::set_device_limits(&mut budget, &cap, device_id, fleet_budget::new_limits(10, DAY, 10, 1, 0));
        let _ = &mut registry;
        owner_end(budget, cap, registry);
        draw(&mut scenario, 10, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EOverFloat)]
    fun lowered_device_limits_bind_immediately() {
        let (mut scenario, device_id) = enrolled();
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        fleet_budget::set_device_limits(&mut budget, &cap, device_id, fleet_budget::new_limits(10, DAY, 10, 1, 0));
        let _ = &mut registry;
        owner_end(budget, cap, registry);
        draw(&mut scenario, 11, 1_000);
        ts::end(scenario);
    }

    // ── Compromised-device controls ───────────────────────────────────────────

    /// A compromised key draining at the maximum rate obtains at most one
    /// allowance per period, on top of its float.
    #[test]
    fun a_compromised_device_draining_at_maximum_rate_is_bounded() {
        let (mut scenario, _) = enrolled();
        let periods = 5;
        let per_draw = ALLOWANCE / MAX_DRAWS;
        let mut p = 0;
        while (p < periods) {
            let mut i = 0;
            while (i < MAX_DRAWS) {
                draw(&mut scenario, per_draw, 1 + p * DAY + i * INTERVAL);
                i = i + 1;
            };
            p = p + 1;
        };
        assert!(held(&mut scenario, DEVICE) == FLOAT + periods * ALLOWANCE, 0);
        assert!(balance(&mut scenario) == DEPOSIT - FLOAT - periods * ALLOWANCE, 1);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = sui_edge::device_cap::EInactiveIdentity)]
    fun revocation_stops_draws_immediately() {
        let (mut scenario, _) = enrolled();
        draw(&mut scenario, 1, 1_000);
        revoke(&mut scenario);
        // The compromised key retries after revocation.
        draw(&mut scenario, 1, 1_000 + INTERVAL);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = sui_edge::device_cap::EInactiveIdentity)]
    fun a_rotated_away_key_cannot_draw() {
        let (mut scenario, _) = enrolled();
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        let owner_cap = ts::take_from_address<DeviceOwnerCap>(&scenario, OWNER);
        device_registry::rotate_device_key(&mut registry, &owner_cap, NEW_KEY);
        ts::return_to_address(OWNER, owner_cap);
        let _ = &mut budget;
        owner_end(budget, cap, registry);
        draw(&mut scenario, 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = sui_edge::device_cap::EWrongSigner)]
    fun a_stolen_cap_cannot_draw_for_another_sender() {
        let (mut scenario, _) = enrolled();
        draw_as(&mut scenario, STRANGER, DEVICE, 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EPaused)]
    fun a_fleet_wide_pause_stops_draws() {
        let (mut scenario, _) = enrolled();
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        fleet_budget::set_paused(&mut budget, &cap, true);
        let _ = &mut registry;
        owner_end(budget, cap, registry);
        draw(&mut scenario, 1, 1_000);
        ts::end(scenario);
    }

    #[test]
    fun unpausing_resumes_draws() {
        let (mut scenario, _) = enrolled();
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        fleet_budget::set_paused(&mut budget, &cap, true);
        let _ = &mut registry;
        owner_end(budget, cap, registry);
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        fleet_budget::set_paused(&mut budget, &cap, false);
        let _ = &mut registry;
        owner_end(budget, cap, registry);
        draw(&mut scenario, 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ESuspended)]
    fun a_suspended_device_cannot_draw() {
        let (mut scenario, device_id) = enrolled();
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        fleet_budget::set_device_suspended(&mut budget, &cap, device_id, true);
        let _ = &mut registry;
        owner_end(budget, cap, registry);
        draw(&mut scenario, 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ENotEnrolled)]
    fun a_removed_device_cannot_draw() {
        let (mut scenario, device_id) = enrolled();
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        fleet_budget::remove_device(&mut budget, &cap, device_id);
        let _ = &mut registry;
        owner_end(budget, cap, registry);
        draw(&mut scenario, 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ENotEnrolled)]
    fun an_unenrolled_registered_device_cannot_draw() {
        let (mut scenario, _) = enrolled();
        activate(&mut scenario, DEVICE_2);
        draw_as(&mut scenario, DEVICE_2, DEVICE_2, 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = sui_edge::device_cap::EInactiveIdentity)]
    fun a_deregistered_device_cannot_draw() {
        let (mut scenario, _) = enrolled();
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        let owner_cap = ts::take_from_address<DeviceOwnerCap>(&scenario, OWNER);
        device_registry::deregister_activated_device(&mut registry, owner_cap, ts::ctx(&mut scenario));
        let _ = &mut budget;
        owner_end(budget, cap, registry);
        draw(&mut scenario, 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ENotOwner)]
    fun a_device_cannot_withdraw() {
        let (mut scenario, _) = enrolled();
        // The device has no budget cap; a fresh budget's cap is the closest it can get.
        ts::next_tx(&mut scenario, DEVICE);
        let mut budget = ts::take_shared<FleetBudget>(&scenario);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let forged = fleet_budget::create(&registry, coin::zero<SUI>(ts::ctx(&mut scenario)), limits(), 0, ts::ctx(&mut scenario));
        transfer::public_transfer(fleet_budget::withdraw(&mut budget, &forged, 1, ts::ctx(&mut scenario)), DEVICE);
        transfer::public_transfer(forged, DEVICE);
        ts::return_shared(registry);
        ts::return_shared(budget);
        ts::end(scenario);
    }

    #[test]
    fun owner_refill_reaches_an_empty_device() {
        let (mut scenario, device_id) = enrolled();
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        fleet_budget::fund_device(&mut budget, &cap, &registry, device_id, FLOAT, ts::ctx(&mut scenario));
        let _ = &mut registry;
        owner_end(budget, cap, registry);
        assert!(held(&mut scenario, DEVICE) == 2 * FLOAT, 0);
        ts::end(scenario);
    }

    // ── Package versions ──────────────────────────────────────────────────────

    /// After a later upgrade migrates the budget, this (older) package
    /// version can no longer draw, as simulated by advancing the budget.
    #[test, expected_failure(abort_code = fleet_budget::EWrongVersion)]
    fun an_older_package_version_cannot_draw_after_migration() {
        let (mut scenario, _) = enrolled();
        ts::next_tx(&mut scenario, OWNER);
        let mut budget = ts::take_shared<FleetBudget>(&scenario);
        fleet_budget::set_version_for_testing(&mut budget, fleet_budget::package_version() + 1);
        ts::return_shared(budget);
        draw(&mut scenario, 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EWrongVersion)]
    fun an_older_package_version_cannot_manage_after_migration() {
        let (mut scenario, _) = enrolled();
        ts::next_tx(&mut scenario, OWNER);
        let mut budget = ts::take_shared<FleetBudget>(&scenario);
        fleet_budget::set_version_for_testing(&mut budget, fleet_budget::package_version() + 1);
        ts::return_shared(budget);
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        transfer::public_transfer(fleet_budget::withdraw(&mut budget, &cap, 1, ts::ctx(&mut scenario)), OWNER);
        let _ = &mut registry;
        owner_end(budget, cap, registry);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ENotUpgrade)]
    fun migrate_requires_a_newer_package() {
        let mut scenario = setup();
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        fleet_budget::migrate(&mut budget, &cap);
        let _ = &mut registry;
        owner_end(budget, cap, registry);
        ts::end(scenario);
    }

    #[test]
    fun migrate_moves_an_older_budget_forward() {
        let mut scenario = setup();
        ts::next_tx(&mut scenario, OWNER);
        let mut budget = ts::take_shared<FleetBudget>(&scenario);
        fleet_budget::set_version_for_testing(&mut budget, 0);
        ts::return_shared(budget);
        let (mut budget, cap, mut registry) = owner_begin(&mut scenario);
        fleet_budget::migrate(&mut budget, &cap);
        assert!(fleet_budget::version(&budget) == fleet_budget::package_version(), 0);
        let _ = &mut registry;
        owner_end(budget, cap, registry);
        ts::end(scenario);
    }
}
