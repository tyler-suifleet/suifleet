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
    use sui_edge::fleet_budget::{Self, FleetBudget, BudgetOwnerCap, GasAccount, GasDrawn, BudgetLow, DeviceFunded, ReserveChanged};

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
    /// Enough for five periods of draws at the allowance.
    const RESERVE: u64 = 500_000_000;

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

    /// Enroll `device_id` (whose owner cap the owner holds) with a float and reserve.
    fun enroll_with(scenario: &mut ts::Scenario, device_id: ID, float: u64, reserve: u64): ID {
        ts::next_tx(scenario, OWNER);
        let mut budget = ts::take_shared<FleetBudget>(scenario);
        let cap = ts::take_from_address<BudgetOwnerCap>(scenario, OWNER);
        let registry = ts::take_shared<DeviceRegistry>(scenario);
        let owner_cap = owner_cap_of(scenario, device_id);
        let clock = clock_at(scenario, 1);
        let account_id = fleet_budget::enroll_device(&mut budget, &cap, &registry, &owner_cap, float, reserve, &clock, ts::ctx(scenario));
        clock::destroy_for_testing(clock);
        ts::return_to_address(OWNER, owner_cap);
        ts::return_shared(registry);
        ts::return_to_address(OWNER, cap);
        ts::return_shared(budget);
        account_id
    }

    fun owner_cap_of(scenario: &ts::Scenario, device_id: ID): DeviceOwnerCap {
        let ids = ts::ids_for_address<DeviceOwnerCap>(OWNER);
        let mut i = 0;
        while (i < ids.length()) {
            let owner_cap = ts::take_from_address_by_id<DeviceOwnerCap>(scenario, OWNER, ids[i]);
            if (device_registry::owner_cap_device_id(&owner_cap) == device_id) return owner_cap;
            ts::return_to_address(OWNER, owner_cap);
            i = i + 1;
        };
        abort 99
    }

    fun enroll(scenario: &mut ts::Scenario, device_id: ID, float: u64): ID {
        enroll_with(scenario, device_id, float, RESERVE)
    }

    /// One device, activated and enrolled with a full float and RESERVE.
    fun enrolled(): (ts::Scenario, ID) {
        let mut scenario = setup();
        let device_id = activate(&mut scenario, DEVICE);
        enroll(&mut scenario, device_id, FLOAT);
        (scenario, device_id)
    }

    fun account_id_of(scenario: &mut ts::Scenario, device_id: ID): ID {
        ts::next_tx(scenario, OWNER);
        let budget = ts::take_shared<FleetBudget>(scenario);
        let id = fleet_budget::account_id(&budget, device_id).destroy_some();
        ts::return_shared(budget);
        id
    }

    /// `sender` draws `amount` with `holder`'s DeviceCap from `account_id` at `now`.
    fun draw_from(scenario: &mut ts::Scenario, sender: address, holder: address, account_id: ID, amount: u64, now: u64) {
        ts::next_tx(scenario, sender);
        let budget = ts::take_shared<FleetBudget>(scenario);
        let mut account = ts::take_shared_by_id<GasAccount>(scenario, account_id);
        let registry = ts::take_shared<DeviceRegistry>(scenario);
        let cap = ts::take_from_address<DeviceCap>(scenario, holder);
        let clock = clock_at(scenario, now);
        fleet_budget::draw_gas(&budget, &mut account, &registry, &cap, amount, &clock, ts::ctx(scenario));
        clock::destroy_for_testing(clock);
        ts::return_to_address(holder, cap);
        ts::return_shared(registry);
        ts::return_shared(account);
        ts::return_shared(budget);
    }

    /// `sender` draws with `holder`'s cap from that device's current account.
    fun draw_as(scenario: &mut ts::Scenario, sender: address, holder: address, amount: u64, now: u64) {
        ts::next_tx(scenario, holder);
        let cap = ts::take_from_address<DeviceCap>(scenario, holder);
        let device_id = device_cap::device_id(&cap);
        ts::return_to_address(holder, cap);
        let account_id = account_id_of(scenario, device_id);
        draw_from(scenario, sender, holder, account_id, amount, now)
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

    fun reserve_of(scenario: &mut ts::Scenario, account_id: ID): u64 {
        ts::next_tx(scenario, OWNER);
        let account = ts::take_shared_by_id<GasAccount>(scenario, account_id);
        let value = fleet_budget::account_reserve(&account);
        ts::return_shared(account);
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

    fun suspend(scenario: &mut ts::Scenario, account_id: ID, suspended: bool) {
        let (budget, cap, registry) = owner_begin(scenario);
        let mut account = ts::take_shared_by_id<GasAccount>(scenario, account_id);
        fleet_budget::set_device_suspended(&budget, &cap, &mut account, suspended);
        ts::return_shared(account);
        owner_end(budget, cap, registry);
    }

    fun remove(scenario: &mut ts::Scenario, account_id: ID) {
        let (mut budget, cap, registry) = owner_begin(scenario);
        let mut account = ts::take_shared_by_id<GasAccount>(scenario, account_id);
        fleet_budget::remove_device(&mut budget, &cap, &mut account);
        ts::return_shared(account);
        owner_end(budget, cap, registry);
    }

    fun set_limits(scenario: &mut ts::Scenario, account_id: ID, limits: fleet_budget::Limits) {
        let (budget, cap, registry) = owner_begin(scenario);
        let mut account = ts::take_shared_by_id<GasAccount>(scenario, account_id);
        fleet_budget::set_device_limits(&budget, &cap, &mut account, limits);
        ts::return_shared(account);
        owner_end(budget, cap, registry);
    }

    // ── Ownership and funding ─────────────────────────────────────────────────

    #[test]
    fun enrollment_pays_the_float_and_allocates_the_reserve() {
        let (mut scenario, device_id) = enrolled();
        assert!(held(&mut scenario, DEVICE) == FLOAT, 0);
        assert!(balance(&mut scenario) == DEPOSIT - FLOAT - RESERVE, 1);
        let account_id = account_id_of(&mut scenario, device_id);
        ts::next_tx(&mut scenario, OWNER);
        let budget = ts::take_shared<FleetBudget>(&scenario);
        let account = ts::take_shared_by_id<GasAccount>(&scenario, account_id);
        assert!(fleet_budget::account_total_drawn(&account) == FLOAT && fleet_budget::account_drawn_in_period(&account) == 0, 2);
        assert!(fleet_budget::account_reserve(&account) == RESERVE && fleet_budget::account_device_id(&account) == device_id, 3);
        assert!(fleet_budget::account_budget_id(&account) == object::id(&budget) && !fleet_budget::account_closed(&account), 4);
        assert!(fleet_budget::device_count(&budget) == 1 && fleet_budget::total_paid(&budget) == FLOAT, 5);
        assert!(fleet_budget::total_allocated(&budget) == RESERVE && fleet_budget::total_reclaimed(&budget) == 0, 6);
        assert!(fleet_budget::spendable(&budget) == DEPOSIT - FLOAT - RESERVE && fleet_budget::reserved(&budget) == 0, 7);
        assert!(fleet_budget::interface_version() == 1 && fleet_budget::version(&budget) == fleet_budget::package_version(), 8);
        assert!(fleet_budget::is_enrolled(&budget, device_id), 9);
        ts::return_shared(account);
        ts::return_shared(budget);
        ts::end(scenario);
    }

    #[test]
    fun owner_deposits_and_withdraws_unused_funds() {
        let mut scenario = setup();
        let (mut budget, cap, registry) = owner_begin(&mut scenario);
        fleet_budget::deposit(&mut budget, &cap, coin::mint_for_testing<SUI>(5, ts::ctx(&mut scenario)));
        let coin = fleet_budget::withdraw(&mut budget, &cap, DEPOSIT, ts::ctx(&mut scenario));
        assert!(coin.value() == DEPOSIT, 0);
        transfer::public_transfer(coin, OWNER);
        assert!(fleet_budget::balance(&budget) == 5 && fleet_budget::total_withdrawn(&budget) == DEPOSIT, 1);
        assert!(fleet_budget::total_deposited(&budget) == DEPOSIT + 5, 2);
        // Falling below the threshold is reported.
        assert!(event::events_by_type<BudgetLow>().length() == 1, 3);
        owner_end(budget, cap, registry);
        ts::end(scenario);
    }

    #[test]
    fun owner_moves_funds_between_budget_and_reserve() {
        let (mut scenario, device_id) = enrolled();
        let account_id = account_id_of(&mut scenario, device_id);
        let (mut budget, cap, registry) = owner_begin(&mut scenario);
        let mut account = ts::take_shared_by_id<GasAccount>(&scenario, account_id);
        fleet_budget::fund_reserve(&mut budget, &cap, &mut account, 7);
        fleet_budget::reclaim_reserve(&mut budget, &cap, &mut account, RESERVE + 7);
        assert!(fleet_budget::account_reserve(&account) == 0, 0);
        assert!(fleet_budget::balance(&budget) == DEPOSIT - FLOAT, 1);
        assert!(event::events_by_type<ReserveChanged>().length() == 2, 2);
        // Everything not paid to the device can be withdrawn.
        transfer::public_transfer(fleet_budget::withdraw(&mut budget, &cap, DEPOSIT - FLOAT, ts::ctx(&mut scenario)), OWNER);
        ts::return_shared(account);
        owner_end(budget, cap, registry);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EOverFloat)]
    fun enrollment_float_is_capped() {
        let mut scenario = setup();
        let device_id = activate(&mut scenario, DEVICE);
        enroll(&mut scenario, device_id, FLOAT + 1);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EInsufficientBalance)]
    fun enrollment_reserve_comes_from_the_budget() {
        let mut scenario = setup();
        let device_id = activate(&mut scenario, DEVICE);
        enroll_with(&mut scenario, device_id, 0, DEPOSIT + 1);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EAlreadyEnrolled)]
    fun a_device_enrolls_once() {
        let (mut scenario, device_id) = enrolled();
        enroll(&mut scenario, device_id, 0);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EInactiveDevice)]
    fun a_revoked_device_cannot_be_enrolled() {
        let mut scenario = setup();
        let device_id = activate(&mut scenario, DEVICE);
        revoke(&mut scenario);
        enroll(&mut scenario, device_id, FLOAT);
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
        let account_id = account_id_of(&mut scenario, device_id);
        ts::next_tx(&mut scenario, OWNER);
        let account = ts::take_shared_by_id<GasAccount>(&scenario, account_id);
        assert!(fleet_budget::account_drawn_in_period(&account) == 60_000_000, 2);
        assert!(fleet_budget::account_draws_in_period(&account) == 2, 3);
        assert!(fleet_budget::account_reserve(&account) == RESERVE - 60_000_000, 4);
        ts::return_shared(account);
        ts::end(scenario);
    }

    /// Draws take the budget by immutable reference: they leave its balance
    /// and totals as they were, so devices never write the fleet object.
    #[test]
    fun draws_leave_the_fleet_budget_unchanged() {
        let (mut scenario, _) = enrolled();
        let before = balance(&mut scenario);
        draw(&mut scenario, FLOAT, 1_000);
        draw(&mut scenario, FLOAT, 1_000 + INTERVAL);
        ts::next_tx(&mut scenario, OWNER);
        let budget = ts::take_shared<FleetBudget>(&scenario);
        assert!(fleet_budget::balance(&budget) == before, 0);
        assert!(fleet_budget::total_paid(&budget) == FLOAT && fleet_budget::total_allocated(&budget) == RESERVE, 1);
        ts::return_shared(budget);
        ts::end(scenario);
    }

    #[test]
    fun draw_event_is_emitted_in_the_drawing_transaction() {
        let (mut scenario, device_id) = enrolled();
        let account_id = account_id_of(&mut scenario, device_id);
        ts::next_tx(&mut scenario, DEVICE);
        let budget = ts::take_shared<FleetBudget>(&scenario);
        let mut account = ts::take_shared_by_id<GasAccount>(&scenario, account_id);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let cap = ts::take_from_address<DeviceCap>(&scenario, DEVICE);
        let clock = clock_at(&mut scenario, 5);
        fleet_budget::draw_gas(&budget, &mut account, &registry, &cap, 1, &clock, ts::ctx(&mut scenario));
        assert!(event::events_by_type<GasDrawn>().length() == 1, 0);
        assert!(event::events_by_type<DeviceFunded>().length() == 0, 1);
        clock::destroy_for_testing(clock);
        ts::return_to_address(DEVICE, cap);
        ts::return_shared(registry);
        ts::return_shared(account);
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

    /// A device draws only from its own reserve, even when the budget holds more.
    #[test, expected_failure(abort_code = fleet_budget::EInsufficientBalance)]
    fun draws_cannot_exceed_the_reserve() {
        let mut scenario = setup();
        let device_id = activate(&mut scenario, DEVICE);
        enroll_with(&mut scenario, device_id, 0, 10);
        draw(&mut scenario, 11, 1_000);
        ts::end(scenario);
    }

    #[test]
    fun owner_limits_apply_per_device() {
        let (mut scenario, device_id) = enrolled();
        let account_id = account_id_of(&mut scenario, device_id);
        set_limits(&mut scenario, account_id, fleet_budget::new_limits(10, DAY, 10, 1, 0));
        draw(&mut scenario, 10, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EOverFloat)]
    fun lowered_device_limits_bind_immediately() {
        let (mut scenario, device_id) = enrolled();
        let account_id = account_id_of(&mut scenario, device_id);
        set_limits(&mut scenario, account_id, fleet_budget::new_limits(10, DAY, 10, 1, 0));
        draw(&mut scenario, 11, 1_000);
        ts::end(scenario);
    }

    // ── Separate accounts ─────────────────────────────────────────────────────

    /// Two devices draw from separate accounts: one draining its own reserve
    /// and allowance leaves the other's untouched.
    #[test]
    fun devices_draw_from_separate_accounts() {
        let mut scenario = setup();
        let first = activate(&mut scenario, DEVICE);
        let second = activate(&mut scenario, DEVICE_2);
        let a = enroll_with(&mut scenario, first, 0, ALLOWANCE);
        let b = enroll_with(&mut scenario, second, 0, ALLOWANCE);
        assert!(a != b, 0);
        draw_as(&mut scenario, DEVICE, DEVICE, FLOAT, 1_000);
        draw_as(&mut scenario, DEVICE, DEVICE, FLOAT, 1_000 + INTERVAL);
        assert!(reserve_of(&mut scenario, a) == 0 && reserve_of(&mut scenario, b) == ALLOWANCE, 1);
        draw_as(&mut scenario, DEVICE_2, DEVICE_2, FLOAT, 1_000 + INTERVAL);
        assert!(held(&mut scenario, DEVICE) == ALLOWANCE && held(&mut scenario, DEVICE_2) == FLOAT, 2);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EWrongAccount)]
    fun a_device_cannot_draw_from_another_devices_account() {
        let mut scenario = setup();
        let first = activate(&mut scenario, DEVICE);
        let second = activate(&mut scenario, DEVICE_2);
        enroll(&mut scenario, first, 0);
        let other = enroll(&mut scenario, second, 0);
        draw_from(&mut scenario, DEVICE, DEVICE, other, 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::EWrongAccount)]
    fun an_account_of_another_budget_is_refused() {
        let (mut scenario, device_id) = enrolled();
        let account_id = account_id_of(&mut scenario, device_id);
        ts::next_tx(&mut scenario, OWNER);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let other = fleet_budget::create(&registry, coin::zero<SUI>(ts::ctx(&mut scenario)), limits(), 0, ts::ctx(&mut scenario));
        ts::return_shared(registry);
        transfer::public_transfer(other, OWNER);
        ts::next_tx(&mut scenario, DEVICE);
        let other_id = ts::most_recent_id_shared<FleetBudget>().destroy_some();
        let budget = ts::take_shared_by_id<FleetBudget>(&scenario, other_id);
        let mut account = ts::take_shared_by_id<GasAccount>(&scenario, account_id);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let cap = ts::take_from_address<DeviceCap>(&scenario, DEVICE);
        let clock = clock_at(&mut scenario, 1_000);
        fleet_budget::draw_gas(&budget, &mut account, &registry, &cap, 1, &clock, ts::ctx(&mut scenario));
        clock::destroy_for_testing(clock);
        ts::return_to_address(DEVICE, cap);
        ts::return_shared(registry);
        ts::return_shared(account);
        ts::return_shared(budget);
        ts::end(scenario);
    }

    /// Removal retires the account for good: re-enrolling creates a new one
    /// and the old one, though still shared, never draws again.
    #[test, expected_failure(abort_code = fleet_budget::ENotEnrolled)]
    fun a_retired_account_cannot_draw_after_re_enrollment() {
        let (mut scenario, device_id) = enrolled();
        let old = account_id_of(&mut scenario, device_id);
        remove(&mut scenario, old);
        assert!(reserve_of(&mut scenario, old) == 0, 0);
        let new = enroll(&mut scenario, device_id, 0);
        assert!(new != old, 1);
        draw(&mut scenario, 1, 1_000);
        draw_from(&mut scenario, DEVICE, DEVICE, old, 1, 1_000 + INTERVAL);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ENotOwner)]
    fun a_device_cannot_reclaim_or_fund_a_reserve() {
        let (mut scenario, device_id) = enrolled();
        let account_id = account_id_of(&mut scenario, device_id);
        ts::next_tx(&mut scenario, DEVICE);
        let mut budget = ts::take_shared<FleetBudget>(&scenario);
        let mut account = ts::take_shared_by_id<GasAccount>(&scenario, account_id);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let forged = fleet_budget::create(&registry, coin::zero<SUI>(ts::ctx(&mut scenario)), limits(), 0, ts::ctx(&mut scenario));
        fleet_budget::reclaim_reserve(&mut budget, &forged, &mut account, 1);
        transfer::public_transfer(forged, DEVICE);
        ts::return_shared(registry);
        ts::return_shared(account);
        ts::return_shared(budget);
        ts::end(scenario);
    }

    // ── Compromised-device controls ───────────────────────────────────────────

    /// A compromised key draining at the maximum rate obtains at most one
    /// allowance per period, on top of its float, and never more than its
    /// reserve; the rest of the budget is out of its reach.
    #[test]
    fun a_compromised_device_draining_at_maximum_rate_is_bounded() {
        let (mut scenario, device_id) = enrolled();
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
        let account_id = account_id_of(&mut scenario, device_id);
        assert!(reserve_of(&mut scenario, account_id) == RESERVE - periods * ALLOWANCE, 1);
        assert!(balance(&mut scenario) == DEPOSIT - FLOAT - RESERVE, 2);
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
        let (budget, cap, mut registry) = owner_begin(&mut scenario);
        let owner_cap = ts::take_from_address<DeviceOwnerCap>(&scenario, OWNER);
        device_registry::rotate_device_key(&mut registry, &owner_cap, NEW_KEY);
        ts::return_to_address(OWNER, owner_cap);
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
        let (mut budget, cap, registry) = owner_begin(&mut scenario);
        fleet_budget::set_paused(&mut budget, &cap, true);
        owner_end(budget, cap, registry);
        draw(&mut scenario, 1, 1_000);
        ts::end(scenario);
    }

    #[test]
    fun unpausing_resumes_draws() {
        let (mut scenario, _) = enrolled();
        let (mut budget, cap, registry) = owner_begin(&mut scenario);
        fleet_budget::set_paused(&mut budget, &cap, true);
        owner_end(budget, cap, registry);
        let (mut budget, cap, registry) = owner_begin(&mut scenario);
        fleet_budget::set_paused(&mut budget, &cap, false);
        owner_end(budget, cap, registry);
        draw(&mut scenario, 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ESuspended)]
    fun a_suspended_device_cannot_draw() {
        let (mut scenario, device_id) = enrolled();
        let account_id = account_id_of(&mut scenario, device_id);
        suspend(&mut scenario, account_id, true);
        draw(&mut scenario, 1, 1_000);
        ts::end(scenario);
    }

    #[test]
    fun a_resumed_device_draws_again() {
        let (mut scenario, device_id) = enrolled();
        let account_id = account_id_of(&mut scenario, device_id);
        suspend(&mut scenario, account_id, true);
        suspend(&mut scenario, account_id, false);
        draw(&mut scenario, 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ENotEnrolled)]
    fun a_removed_device_cannot_draw() {
        let (mut scenario, device_id) = enrolled();
        let account_id = account_id_of(&mut scenario, device_id);
        remove(&mut scenario, account_id);
        assert!(balance(&mut scenario) == DEPOSIT - FLOAT, 0);
        draw_from(&mut scenario, DEVICE, DEVICE, account_id, 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ENotEnrolled)]
    fun a_removed_device_cannot_be_suspended_or_resumed() {
        let (mut scenario, device_id) = enrolled();
        let account_id = account_id_of(&mut scenario, device_id);
        remove(&mut scenario, account_id);
        suspend(&mut scenario, account_id, false);
        ts::end(scenario);
    }

    /// An unenrolled device has no account; an enrolled device's is not its own.
    #[test, expected_failure(abort_code = fleet_budget::EWrongAccount)]
    fun an_unenrolled_registered_device_cannot_draw() {
        let (mut scenario, device_id) = enrolled();
        let second = activate(&mut scenario, DEVICE_2);
        ts::next_tx(&mut scenario, OWNER);
        let budget = ts::take_shared<FleetBudget>(&scenario);
        assert!(!fleet_budget::is_enrolled(&budget, second) && fleet_budget::account_id(&budget, second).is_none(), 0);
        ts::return_shared(budget);
        let account_id = account_id_of(&mut scenario, device_id);
        draw_from(&mut scenario, DEVICE_2, DEVICE_2, account_id, 1, 1_000);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = sui_edge::device_cap::EInactiveIdentity)]
    fun a_deregistered_device_cannot_draw() {
        let (mut scenario, _) = enrolled();
        let (budget, cap, mut registry) = owner_begin(&mut scenario);
        let owner_cap = ts::take_from_address<DeviceOwnerCap>(&scenario, OWNER);
        device_registry::deregister_activated_device(&mut registry, owner_cap, ts::ctx(&mut scenario));
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
        let account_id = account_id_of(&mut scenario, device_id);
        let (mut budget, cap, registry) = owner_begin(&mut scenario);
        let mut account = ts::take_shared_by_id<GasAccount>(&scenario, account_id);
        fleet_budget::fund_device(&mut budget, &cap, &registry, &mut account, FLOAT, ts::ctx(&mut scenario));
        assert!(fleet_budget::account_total_drawn(&account) == 2 * FLOAT, 0);
        ts::return_shared(account);
        owner_end(budget, cap, registry);
        assert!(held(&mut scenario, DEVICE) == 2 * FLOAT, 1);
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
        let (mut budget, cap, registry) = owner_begin(&mut scenario);
        transfer::public_transfer(fleet_budget::withdraw(&mut budget, &cap, 1, ts::ctx(&mut scenario)), OWNER);
        owner_end(budget, cap, registry);
        ts::end(scenario);
    }

    /// Accounts follow their budget's version gate.
    #[test, expected_failure(abort_code = fleet_budget::EWrongVersion)]
    fun an_older_package_version_cannot_change_an_account_after_migration() {
        let (mut scenario, device_id) = enrolled();
        let account_id = account_id_of(&mut scenario, device_id);
        ts::next_tx(&mut scenario, OWNER);
        let mut budget = ts::take_shared<FleetBudget>(&scenario);
        fleet_budget::set_version_for_testing(&mut budget, fleet_budget::package_version() + 1);
        ts::return_shared(budget);
        suspend(&mut scenario, account_id, true);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = fleet_budget::ENotUpgrade)]
    fun migrate_requires_a_newer_package() {
        let mut scenario = setup();
        let (mut budget, cap, registry) = owner_begin(&mut scenario);
        fleet_budget::migrate(&mut budget, &cap);
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
        let (mut budget, cap, registry) = owner_begin(&mut scenario);
        fleet_budget::migrate(&mut budget, &cap);
        assert!(fleet_budget::version(&budget) == fleet_budget::package_version(), 0);
        owner_end(budget, cap, registry);
        ts::end(scenario);
    }
}
