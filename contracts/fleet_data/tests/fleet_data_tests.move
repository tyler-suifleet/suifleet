#[test_only]
module fleet_data::fleet_data_tests {
    use std::string;
    use std::unit_test::destroy;
    use sui::clock::{Self, Clock};
    use sui::coin;
    use sui::sui::SUI;
    use sui::test_scenario::{Self as ts};
    use sui_edge::device_cap::{Self, DeviceCap};
    use sui_edge::device_registry::{Self, DeviceRegistry, DeviceOwnerCap};
    use sui_edge::fleet_budget::{Self, FleetBudget, BudgetOwnerCap, GasAccount};
    use fleet_storage::fleet_storage::{Self, FleetStorage};
    use fleet_data::fleet_data::{Self, FleetData, DeviceData};
    use wal::wal::WAL;
    use walrus::blob;
    use walrus::system::System;

    const OWNER: address = @0xA1;
    const DEVICE: address = @0xD1;
    const SUPPORT: address = @0xB1;
    const DAY: u64 = 86_400_000;

    fun zeros(n: u64): vector<u8> { let mut v = vector[]; while (v.length() < n) v.push_back(0); v }
    fun clock_at(s: &mut ts::Scenario, at: u64): Clock {
        let mut c = clock::create_for_testing(ts::ctx(s));
        c.set_for_testing(at); c
    }
    fun setup(): (ts::Scenario, System) {
        let mut s = ts::begin(OWNER);
        device_registry::init_for_testing(ts::ctx(&mut s));
        let mut system = walrus::system::new_for_testing(ts::ctx(&mut s));
        ts::next_tx(&mut s, OWNER);
        let mut registry = ts::take_shared<DeviceRegistry>(&s);
        let cap = fleet_budget::create(&registry, coin::mint_for_testing<SUI>(1_000, ts::ctx(&mut s)),
            fleet_budget::new_limits(10, DAY, 10, 1, 0), 0, ts::ctx(&mut s));
        let clock = clock_at(&mut s, 1);
        let device = device_registry::register_device(&mut registry, string::utf8(b"test"), DEVICE,
            string::utf8(b"aarch64"), &clock, ts::ctx(&mut s));
        transfer::public_transfer(device_cap::issue(&device, ts::ctx(&mut s)), DEVICE);
        let owner_cap = device_registry::activate_device(&mut registry, device, ts::ctx(&mut s));
        transfer::public_transfer(owner_cap, OWNER);
        clock::destroy_for_testing(clock);
        ts::return_shared(registry);
        ts::next_tx(&mut s, OWNER);
        let mut budget = ts::take_shared<FleetBudget>(&s);
        let registry = ts::take_shared<DeviceRegistry>(&s);
        let owner_cap = ts::take_from_address<DeviceOwnerCap>(&s, OWNER);
        let clock = clock_at(&mut s, 1);
        fleet_budget::enroll_device(&mut budget, &cap, &registry, &owner_cap, 0, 0, &clock, ts::ctx(&mut s));
        fleet_storage::create(&budget, &cap, fleet_storage::new_quota(DAY, 500_000_000, 10, 100_000, 1_000_000), ts::ctx(&mut s));
        clock::destroy_for_testing(clock);
        ts::return_shared(registry); ts::return_shared(budget);
        ts::return_to_address(OWNER, owner_cap);
        ts::next_tx(&mut s, OWNER);
        let budget = ts::take_shared<FleetBudget>(&s);
        let mut storage = ts::take_shared<FleetStorage>(&s);
        let mut payment = coin::mint_for_testing<WAL>(1_000_000_000, ts::ctx(&mut s));
        let pool = system.reserve_space(500_000_000, 5, &mut payment, ts::ctx(&mut s));
        destroy(payment);
        fleet_storage::deposit_storage(&mut storage, &budget, &cap, pool);
        fleet_storage::deposit_wal(&mut storage, &budget, &cap, coin::mint_for_testing<WAL>(1_000_000, ts::ctx(&mut s)));
        fleet_data::create(&budget, &cap, &storage, string::utf8(b"4c78adac"), ts::ctx(&mut s));
        ts::return_shared(budget); ts::return_shared(storage);
        ts::next_tx(&mut s, OWNER);
        let budget = ts::take_shared<FleetBudget>(&s);
        let account = ts::take_shared<GasAccount>(&s);
        let mut fleet = ts::take_shared<FleetData>(&s);
        fleet_data::enroll(&mut fleet, &budget, &cap, &account, ts::ctx(&mut s));
        ts::return_shared(fleet); ts::return_shared(account); ts::return_shared(budget);
        transfer::public_transfer(cap, OWNER);
        (s, system)
    }
    fun health(s: &mut ts::Scenario, seq: u64, at: u64) {
        ts::next_tx(s, DEVICE);
        let fleet = ts::take_shared<FleetData>(s);
        let mut writer = ts::take_shared<DeviceData>(s);
        let budget = ts::take_shared<FleetBudget>(s);
        let account = ts::take_shared<GasAccount>(s);
        let registry = ts::take_shared<DeviceRegistry>(s);
        let cap = ts::take_from_address<DeviceCap>(s, DEVICE);
        let clock = clock_at(s, at);
        fleet_data::report_health(&fleet, &mut writer, &budget, &account, &registry, &cap,
            seq, b"{}", zeros(97), &clock, ts::ctx(s));
        assert!(fleet_data::health_seq(&writer) == seq, 100);
        clock::destroy_for_testing(clock);
        ts::return_shared(fleet); ts::return_shared(writer); ts::return_shared(budget);
        ts::return_shared(account); ts::return_shared(registry); ts::return_to_address(DEVICE, cap);
    }
    fun upload(s: &mut ts::Scenario, system: &mut System, root: u256, certify: bool): ID {
        ts::next_tx(s, DEVICE);
        let mut storage = ts::take_shared<FleetStorage>(s);
        let budget = ts::take_shared<FleetBudget>(s);
        let account = ts::take_shared<GasAccount>(s);
        let registry = ts::take_shared<DeviceRegistry>(s);
        let cap = ts::take_from_address<DeviceCap>(s, DEVICE);
        let clock = clock_at(s, 1);
        let id = fleet_storage::register_blob(&mut storage, &budget, &account, &registry, &cap,
            system, blob::derive_blob_id(root, 1, 100), root, 100, 1, &clock, ts::ctx(s));
        if (certify) fleet_storage::certify_for_testing(&mut storage, system, id);
        clock::destroy_for_testing(clock);
        ts::return_shared(storage); ts::return_shared(budget); ts::return_shared(account);
        ts::return_shared(registry); ts::return_to_address(DEVICE, cap);
        id
    }
    fun anchor(s: &mut ts::Scenario, id: ID, seq: u64, kind: u8, at: u64) {
        ts::next_tx(s, DEVICE);
        let fleet = ts::take_shared<FleetData>(s);
        let mut writer = ts::take_shared<DeviceData>(s);
        let storage = ts::take_shared<FleetStorage>(s);
        let budget = ts::take_shared<FleetBudget>(s);
        let account = ts::take_shared<GasAccount>(s);
        let registry = ts::take_shared<DeviceRegistry>(s);
        let cap = ts::take_from_address<DeviceCap>(s, DEVICE);
        let clock = clock_at(s, at);
        fleet_data::anchor(&fleet, &mut writer, &budget, &account, &registry, &cap, &storage,
            id, seq, kind, zeros(32), &clock, ts::ctx(s));
        clock::destroy_for_testing(clock);
        ts::return_shared(fleet); ts::return_shared(writer); ts::return_shared(storage);
        ts::return_shared(budget); ts::return_shared(account); ts::return_shared(registry);
        ts::return_to_address(DEVICE, cap);
    }
    fun admin(s: &mut ts::Scenario, operation: u8) {
        ts::next_tx(s, OWNER);
        let mut fleet = ts::take_shared<FleetData>(s);
        let mut budget = ts::take_shared<FleetBudget>(s);
        let cap = ts::take_from_address<BudgetOwnerCap>(s, OWNER);
        if (operation == 0) fleet_budget::set_paused(&mut budget, &cap, true)
        else if (operation == 1) fleet_data::set_support(&mut fleet, &budget, &cap, SUPPORT, true)
        else if (operation == 2) fleet_data::set_support(&mut fleet, &budget, &cap, SUPPORT, false)
        else if (operation == 3) fleet_data::rotate_key_epoch(&mut fleet, &budget, &cap)
        else {
            let mut registry = ts::take_shared<DeviceRegistry>(s);
            let owner_cap = ts::take_from_address<DeviceOwnerCap>(s, OWNER);
            device_registry::revoke_device(&mut registry, &owner_cap);
            ts::return_shared(registry); ts::return_to_address(OWNER, owner_cap);
        };
        ts::return_shared(fleet); ts::return_shared(budget); ts::return_to_address(OWNER, cap);
    }
    fun identity(s: &mut ts::Scenario): vector<u8> {
        ts::next_tx(s, OWNER);
        let fleet = ts::take_shared<FleetData>(s);
        let mut id = fleet_data::identity_prefix(&fleet);
        id.append(zeros(32)); ts::return_shared(fleet); id
    }
    fun approve(s: &mut ts::Scenario, id: vector<u8>, sender: address) {
        ts::next_tx(s, sender);
        let fleet = ts::take_shared<FleetData>(s);
        fleet_data::approve_support_for_testing(id, &fleet, ts::ctx(s));
        ts::return_shared(fleet);
    }
    fun finish(s: ts::Scenario, system: System) { destroy(system); ts::end(s); }

    #[test]
    fun health_and_daily_batch() {
        let (mut s, mut system) = setup();
        health(&mut s, 1, 1); health(&mut s, 2, DAY + 1);
        let first = upload(&mut s, &mut system, 42, true);
        anchor(&mut s, first, 1, 0, 1);
        let crash = upload(&mut s, &mut system, 43, true);
        anchor(&mut s, crash, 2, 2, 2);
        let next = upload(&mut s, &mut system, 44, true);
        anchor(&mut s, next, 3, 0, DAY + 1);
        finish(s, system);
    }
    #[test, expected_failure(abort_code = fleet_data::ECadence)]
    fun daily_default_refuses_an_hourly_report() {
        let (mut s, system) = setup();
        health(&mut s, 1, 1); health(&mut s, 2, 3_600_001); finish(s, system);
    }
    #[test, expected_failure(abort_code = fleet_data::ESequence)]
    fun replayed_health() { let (mut s, system) = setup(); health(&mut s, 1, 1); health(&mut s, 1, DAY); finish(s, system); }
    #[test, expected_failure(abort_code = fleet_data::ECadence)]
    fun health_cadence() { let (mut s, system) = setup(); health(&mut s, 1, 1); health(&mut s, 2, 2); finish(s, system); }
    #[test, expected_failure(abort_code = fleet_data::ECadence)]
    fun daily_cadence() {
        let (mut s, mut system) = setup();
        let a = upload(&mut s, &mut system, 42, true); anchor(&mut s, a, 1, 0, 1);
        let b = upload(&mut s, &mut system, 43, true); anchor(&mut s, b, 2, 0, 2); finish(s, system);
    }
    #[test, expected_failure(abort_code = fleet_data::ECadence)]
    fun diagnostic_and_crash_share_limit() {
        let (mut s, mut system) = setup();
        let a = upload(&mut s, &mut system, 42, true); anchor(&mut s, a, 1, 1, 1);
        let b = upload(&mut s, &mut system, 43, true); anchor(&mut s, b, 2, 2, 2); finish(s, system);
    }
    #[test, expected_failure(abort_code = fleet_data::EExists)]
    fun duplicate_blob() {
        let (mut s, mut system) = setup(); let a = upload(&mut s, &mut system, 42, true);
        anchor(&mut s, a, 1, 0, 1); anchor(&mut s, a, 2, 2, DAY); finish(s, system);
    }
    #[test, expected_failure(abort_code = fleet_data::EBlob)]
    fun uncertified_blob() {
        let (mut s, mut system) = setup(); let a = upload(&mut s, &mut system, 42, false);
        anchor(&mut s, a, 1, 0, 1); finish(s, system);
    }
    #[test, expected_failure(abort_code = sui_edge::fleet_budget::EPaused)]
    fun paused_health() { let (mut s, system) = setup(); admin(&mut s, 0); health(&mut s, 1, 1); finish(s, system); }
    #[test, expected_failure(abort_code = sui_edge::device_cap::EInactiveIdentity)]
    fun revoked_health() { let (mut s, system) = setup(); admin(&mut s, 4); health(&mut s, 1, 1); finish(s, system); }
    #[test]
    fun support_read() { let (mut s, system) = setup(); admin(&mut s, 1); let id = identity(&mut s); approve(&mut s, id, SUPPORT); finish(s, system); }
    #[test, expected_failure(abort_code = fleet_data::EAccess)]
    fun device_cannot_read() { let (mut s, system) = setup(); let id = identity(&mut s); approve(&mut s, id, DEVICE); finish(s, system); }
    #[test, expected_failure(abort_code = fleet_data::EAccess)]
    fun revoked_support() { let (mut s, system) = setup(); admin(&mut s, 1); let id = identity(&mut s); admin(&mut s, 2); approve(&mut s, id, SUPPORT); finish(s, system); }
    #[test, expected_failure(abort_code = fleet_data::EAccess)]
    fun retired_key_epoch() { let (mut s, system) = setup(); admin(&mut s, 1); let id = identity(&mut s); admin(&mut s, 3); approve(&mut s, id, SUPPORT); finish(s, system); }
    #[test]
    fun diagnostics_are_durable_and_requested_by_owner() {
        let (mut s, system) = setup();
        ts::next_tx(&mut s, OWNER);
        let fleet = ts::take_shared<FleetData>(&s);
        let mut writer = ts::take_shared<DeviceData>(&s);
        let budget = ts::take_shared<FleetBudget>(&s);
        let cap = ts::take_from_address<BudgetOwnerCap>(&s, OWNER);
        let clock = clock_at(&mut s, 10);
        fleet_data::request_diagnostics(&fleet, &mut writer, &budget, &cap, &clock);
        assert!(fleet_data::diagnostic_seq(&fleet_data::diagnostic_request(&writer).destroy_some()) == 1);
        clock::destroy_for_testing(clock);
        ts::return_shared(fleet); ts::return_shared(writer); ts::return_shared(budget); ts::return_to_address(OWNER, cap);
        ts::next_tx(&mut s, DEVICE);
        let writer = ts::take_shared<DeviceData>(&s);
        assert!(fleet_data::diagnostic_seq(&fleet_data::diagnostic_request(&writer).destroy_some()) == 1);
        ts::return_shared(writer); destroy(system); ts::end(s);
    }
    #[test, expected_failure(abort_code = fleet_data::EAccess)]
    fun device_cannot_request_diagnostics_as_support() {
        let (mut s, system) = setup();
        ts::next_tx(&mut s, DEVICE);
        let fleet = ts::take_shared<FleetData>(&s);
        let mut writer = ts::take_shared<DeviceData>(&s);
        let clock = clock_at(&mut s, 10);
        fleet_data::request_diagnostics_support(&fleet, &mut writer, &clock, ts::ctx(&mut s));
        clock::destroy_for_testing(clock); ts::return_shared(fleet); ts::return_shared(writer);
        destroy(system); ts::end(s);
    }

    #[test]
    fun owner_account_replacement_preserves_health_cadence_and_history() {
        let (mut s, system) = setup();
        health(&mut s, 1, 1);
        ts::next_tx(&mut s, OWNER);
        let mut budget = ts::take_shared<FleetBudget>(&s);
        let cap = ts::take_from_address<BudgetOwnerCap>(&s, OWNER);
        let mut old = ts::take_shared<GasAccount>(&s);
        fleet_budget::remove_device(&mut budget, &cap, &mut old);
        let registry = ts::take_shared<DeviceRegistry>(&s);
        let device_cap = ts::take_from_address<DeviceOwnerCap>(&s, OWNER);
        let clock = clock_at(&mut s, 2);
        fleet_budget::enroll_device(&mut budget, &cap, &registry, &device_cap, 0, 0, &clock, ts::ctx(&mut s));
        clock::destroy_for_testing(clock);
        ts::return_shared(budget); ts::return_shared(old); ts::return_shared(registry);
        ts::return_to_address(OWNER, cap); ts::return_to_address(OWNER, device_cap);
        ts::next_tx(&mut s, OWNER);
        let fleet = ts::take_shared<FleetData>(&s);
        let mut writer = ts::take_shared<DeviceData>(&s);
        let budget = ts::take_shared<FleetBudget>(&s);
        let cap = ts::take_from_address<BudgetOwnerCap>(&s, OWNER);
        let identity_cap = ts::take_from_address<DeviceCap>(&s, DEVICE);
        let account_id = fleet_budget::account_id(&budget, device_cap::device_id(&identity_cap)).destroy_some();
        let account = ts::take_shared_by_id<GasAccount>(&s, account_id);
        fleet_data::replace_account(&fleet, &mut writer, &budget, &cap, &account);
        assert!(fleet_data::health_seq(&writer) == 1);
        assert!(fleet_data::anchor_count(&writer) == 0);
        ts::return_shared(fleet); ts::return_shared(writer); ts::return_shared(budget); ts::return_shared(account);
        ts::return_to_address(OWNER, cap); ts::return_to_address(DEVICE, identity_cap); destroy(system); ts::end(s);
    }

}
