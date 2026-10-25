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
    fun policy(s: &mut ts::Scenario, kind: u8, epochs: u32) {
        ts::next_tx(s, OWNER);
        let mut fleet = ts::take_shared<FleetData>(s);
        let budget = ts::take_shared<FleetBudget>(s);
        let cap = ts::take_from_address<BudgetOwnerCap>(s, OWNER);
        fleet_data::set_stream_retention(&mut fleet, &budget, &cap, kind, epochs);
        assert!(fleet_data::stream_retention(&fleet, kind) == option::some(epochs));
        ts::return_shared(fleet); ts::return_shared(budget); ts::return_to_address(OWNER, cap);
    }
    #[test]
    fun stream_policy_is_independent_and_updates_without_rewriting_history() {
        let (mut s, system) = setup();
        policy(&mut s, 0, 3); policy(&mut s, 1, 13); policy(&mut s, 2, 26); policy(&mut s, 0, 53);
        ts::next_tx(&mut s, OWNER);
        let fleet = ts::take_shared<FleetData>(&s);
        assert!(fleet_data::stream_retention(&fleet, 0) == option::some(53));
        assert!(fleet_data::stream_retention(&fleet, 1) == option::some(13));
        assert!(fleet_data::stream_retention(&fleet, 2) == option::some(26));
        ts::return_shared(fleet); destroy(system); ts::end(s);
    }
    #[test, expected_failure(abort_code = fleet_data::EBounds)]
    fun stream_policy_refuses_zero() { let (mut s, system) = setup(); policy(&mut s, 0, 0); destroy(system); ts::end(s); }
    #[test, expected_failure(abort_code = fleet_data::EBounds)]
    fun stream_policy_refuses_beyond_horizon() { let (mut s, system) = setup(); policy(&mut s, 0, 54); destroy(system); ts::end(s); }
    #[test, expected_failure(abort_code = fleet_data::EBounds)]
    fun stream_policy_refuses_unknown_kind() { let (mut s, system) = setup(); policy(&mut s, 3, 1); destroy(system); ts::end(s); }

    fun stream_upload(s: &mut ts::Scenario, system: &mut System, kind: u8, root: u256): ID {
        ts::next_tx(s, DEVICE);
        let fleet = ts::take_shared<FleetData>(s);
        let writer = ts::take_shared<DeviceData>(s);
        let mut storage = ts::take_shared<FleetStorage>(s);
        let budget = ts::take_shared<FleetBudget>(s);
        let account = ts::take_shared<GasAccount>(s);
        let registry = ts::take_shared<DeviceRegistry>(s);
        let cap = ts::take_from_address<DeviceCap>(s, DEVICE);
        let clock = clock_at(s, 1);
        let before = fleet_storage::pool_epochs(&storage);
        let id = fleet_data::register_stream_blob(&fleet, &writer, &budget, &account,
            &registry, &cap, &mut storage, system, kind, blob::derive_blob_id(root, 1, 100), root, 100, &clock, ts::ctx(s));
        assert!(fleet_storage::pool_epochs(&storage) == before);
        let epochs = fleet_data::stream_retention(&fleet, kind).destroy_some();
        assert!(fleet_storage::record_end_epoch(&fleet_storage::blob_record(&storage, id).destroy_some()) == system.epoch() + epochs);
        fleet_storage::certify_for_testing(&mut storage, system, id);
        clock::destroy_for_testing(clock);
        ts::return_shared(fleet); ts::return_shared(writer); ts::return_shared(storage);
        ts::return_shared(budget); ts::return_shared(account); ts::return_shared(registry); ts::return_to_address(DEVICE, cap);
        id
    }
    #[test]
    fun stream_write_buys_live_policy_without_consuming_pool() {
        let (mut s, mut system) = setup();
        policy(&mut s, 0, 3); policy(&mut s, 1, 13);
        let a = stream_upload(&mut s, &mut system, 0, 500);
        anchor(&mut s, a, 1, 0, DAY);
        let b = stream_upload(&mut s, &mut system, 1, 501);
        anchor(&mut s, b, 2, 1, DAY + 300_000);
        policy(&mut s, 0, 26);
        let c = stream_upload(&mut s, &mut system, 0, 502);
        anchor(&mut s, c, 3, 0, 2 * DAY);
        destroy(system); ts::end(s);
    }

    #[test]
    fun stream_compact_history_keeps_prunable_expiry_index() {
        let (mut s, mut system) = setup(); policy(&mut s, 0, 3);
        let id = stream_upload(&mut s, &mut system, 0, 777);
        let _head = compact(&mut s, id, 1, DAY, vector[]);
        ts::next_tx(&mut s, OWNER);
        let fleet = ts::take_shared<FleetData>(&s);
        let mut writer = ts::take_shared<DeviceData>(&s);
        let budget = ts::take_shared<FleetBudget>(&s);
        let cap = ts::take_from_address<BudgetOwnerCap>(&s, OWNER);
        let mut storage = ts::take_shared<FleetStorage>(&s);
        assert!(fleet_data::has_anchor(&writer, 0));
        fleet_data::prune_at_epoch_for_testing(&fleet, &mut writer, &budget, &cap, &mut storage, 3, vector[0]);
        assert!(!fleet_data::has_anchor(&writer, 0));
        assert!(!fleet_storage::has_blob(&storage, id));
        assert!(fleet_data::compact_head(&writer).is_none());
        assert!(fleet_data::anchor_count(&writer) == 1);
        ts::return_shared(fleet); ts::return_shared(writer); ts::return_shared(budget); ts::return_shared(storage);
        ts::return_to_address(OWNER, cap); destroy(system); ts::end(s);
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
    fun compact(s: &mut ts::Scenario, id: ID, seq: u64, at: u64, previous: vector<u8>): vector<u8> {
        ts::next_tx(s, DEVICE);
        let fleet = ts::take_shared<FleetData>(s);
        let mut writer = ts::take_shared<DeviceData>(s);
        let storage = ts::take_shared<FleetStorage>(s);
        let budget = ts::take_shared<FleetBudget>(s);
        let account = ts::take_shared<GasAccount>(s);
        let registry = ts::take_shared<DeviceRegistry>(s);
        let cap = ts::take_from_address<DeviceCap>(s, DEVICE);
        let clock = clock_at(s, at);
        fleet_data::anchor_compact(&fleet, &mut writer, &budget, &account, &registry, &cap, &storage,
            id, seq, 0, zeros(32), previous, &clock, ts::ctx(s));
        let bytes = sui::bcs::to_bytes(&fleet_data::compact_head(&writer).destroy_some());
        clock::destroy_for_testing(clock);
        ts::return_shared(fleet); ts::return_shared(writer); ts::return_shared(storage);
        ts::return_shared(budget); ts::return_shared(account); ts::return_shared(registry);
        ts::return_to_address(DEVICE, cap);
        bytes
    }
    #[test]
    fun compact_history_commits_previous_head_without_legacy_entries() {
        let (mut s, mut system) = setup();
        let one = upload(&mut s, &mut system, 101, true);
        let first = compact(&mut s, one, 1, DAY, vector[]);
        let two = upload(&mut s, &mut system, 102, true);
        let second = compact(&mut s, two, 2, 2 * DAY, std::hash::sha2_256(first));
        let three = upload(&mut s, &mut system, 103, true);
        let third = compact(&mut s, three, 3, 3 * DAY, std::hash::sha2_256(second));
        // After the initial empty predecessor, the root never grows with history.
        assert!(third.length() < 512);
        destroy(system); ts::end(s);
    }
    #[test, expected_failure(abort_code = fleet_data::ESequence)]
    fun compact_history_refuses_stale_predecessor() {
        let (mut s, mut system) = setup();
        let one = upload(&mut s, &mut system, 101, true);
        let _ = compact(&mut s, one, 1, DAY, vector[]);
        let two = upload(&mut s, &mut system, 102, true);
        let _ = compact(&mut s, two, 2, 2 * DAY, vector[]);
        destroy(system); ts::end(s);
    }
    #[test, expected_failure(abort_code = fleet_data::EBlob)]
    fun compact_history_refuses_uncertified_archive() {
        let (mut s, mut system) = setup();
        let one = upload(&mut s, &mut system, 101, false);
        let _ = compact(&mut s, one, 1, DAY, vector[]);
        destroy(system); ts::end(s);
    }
    // Compact mode deliberately has no unbounded global seen-blob set.
    // Readers must refuse the old blob's ciphertext sequence at the third link.
    #[test]
    fun compact_history_only_deduplicates_current_head() {
        let (mut s, mut system) = setup();
        let one = upload(&mut s, &mut system, 101, true);
        let two = upload(&mut s, &mut system, 102, true);
        let first = compact(&mut s, one, 1, DAY, vector[]);
        let second = compact(&mut s, two, 2, 2 * DAY, std::hash::sha2_256(first));
        let _ = compact(&mut s, one, 3, 3 * DAY, std::hash::sha2_256(second));
        destroy(system); ts::end(s);
    }
    #[test, expected_failure(abort_code = fleet_data::EExists)]
    fun compact_history_refuses_same_blob_twice() {
        let (mut s, mut system) = setup();
        let one = upload(&mut s, &mut system, 101, true);
        let first = compact(&mut s, one, 1, DAY, vector[]);
        let _ = compact(&mut s, one, 2, 2 * DAY, std::hash::sha2_256(first));
        destroy(system); ts::end(s);
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

    // ── SF-129 storage reclaim ──────────────────────────────────────────────
    // The fixture pool covers Walrus epochs [0, 5): blobs expire at epoch 5.

    fun archives(s: &mut ts::Scenario, system: &mut System): (ID, ID) {
        let a = upload(s, system, 42, true); anchor(s, a, 1, 0, 1);
        let b = upload(s, system, 43, true); anchor(s, b, 2, 0, DAY + 1);
        (a, b)
    }
    fun prune(s: &mut ts::Scenario, epoch: u32, indices: vector<u64>) {
        ts::next_tx(s, OWNER);
        let fleet = ts::take_shared<FleetData>(s);
        let mut writer = ts::take_shared<DeviceData>(s);
        let mut storage = ts::take_shared<FleetStorage>(s);
        let budget = ts::take_shared<FleetBudget>(s);
        let cap = ts::take_from_address<BudgetOwnerCap>(s, OWNER);
        fleet_data::prune_at_epoch_for_testing(&fleet, &mut writer, &budget, &cap, &mut storage, epoch, indices);
        ts::return_shared(fleet); ts::return_shared(writer); ts::return_shared(storage);
        ts::return_shared(budget); ts::return_to_address(OWNER, cap);
    }
    fun burn(s: &mut ts::Scenario, epoch: u32, ids: vector<ID>) {
        ts::next_tx(s, OWNER);
        let fleet = ts::take_shared<FleetData>(s);
        let mut storage = ts::take_shared<FleetStorage>(s);
        let budget = ts::take_shared<FleetBudget>(s);
        let cap = ts::take_from_address<BudgetOwnerCap>(s, OWNER);
        fleet_data::burn_at_epoch_for_testing(&fleet, &budget, &cap, &mut storage, epoch, ids);
        ts::return_shared(fleet); ts::return_shared(storage);
        ts::return_shared(budget); ts::return_to_address(OWNER, cap);
    }
    fun release(s: &mut ts::Scenario, id: ID): blob::Blob {
        ts::next_tx(s, OWNER);
        let mut storage = ts::take_shared<FleetStorage>(s);
        let budget = ts::take_shared<FleetBudget>(s);
        let cap = ts::take_from_address<BudgetOwnerCap>(s, OWNER);
        let released = fleet_storage::release_blob(&mut storage, &budget, &cap, id);
        ts::return_shared(storage); ts::return_shared(budget); ts::return_to_address(OWNER, cap);
        released
    }
    fun counts(s: &mut ts::Scenario): (u64, u64, bool, bool) {
        ts::next_tx(s, OWNER);
        let writer = ts::take_shared<DeviceData>(s);
        let storage = ts::take_shared<FleetStorage>(s);
        let (count, blobs) = (fleet_data::anchor_count(&writer), fleet_storage::blob_count(&storage));
        let (first, second) = (fleet_data::has_anchor(&writer, 0), fleet_data::has_anchor(&writer, 1));
        ts::return_shared(writer); ts::return_shared(storage);
        (count, blobs, first, second)
    }

    #[test]
    fun prune_expired_anchor_burns_its_blob_and_keeps_newer_history() {
        let (mut s, mut system) = setup();
        let (a, b) = archives(&mut s, &mut system);
        prune(&mut s, 5, vector[0]);
        let (count, blobs, first, second) = counts(&mut s);
        // The index never shrinks, so later anchors keep their positions.
        assert!(count == 2 && blobs == 1 && !first && second);
        ts::next_tx(&mut s, OWNER);
        let storage = ts::take_shared<FleetStorage>(&s);
        assert!(!fleet_storage::has_blob(&storage, a) && fleet_storage::has_blob(&storage, b));
        ts::return_shared(storage);
        // The next archive appends at the next index, not into the gap.
        let c = upload(&mut s, &mut system, 44, true); anchor(&mut s, c, 3, 0, 2 * DAY + 1);
        let (count, _, _, _) = counts(&mut s);
        assert!(count == 3);
        prune(&mut s, 6, vector[1, 2]);
        let (_, blobs, _, second) = counts(&mut s);
        assert!(blobs == 0 && !second);
        finish(s, system);
    }
    #[test, expected_failure(abort_code = fleet_data::EUnexpired)]
    fun prune_refuses_unexpired_archive() {
        let (mut s, mut system) = setup(); archives(&mut s, &mut system);
        prune(&mut s, 4, vector[0]); finish(s, system);
    }
    #[test, expected_failure(abort_code = fleet_data::EUnexpired)]
    fun prune_batch_is_atomic_when_one_archive_is_unexpired() {
        let (mut s, mut system) = setup(); archives(&mut s, &mut system);
        // An extended second archive must not be destroyed with the first.
        ts::next_tx(&mut s, OWNER);
        let mut storage = ts::take_shared<FleetStorage>(&s);
        let budget = ts::take_shared<FleetBudget>(&s);
        let cap = ts::take_from_address<BudgetOwnerCap>(&s, OWNER);
        let b = second_blob(&s);
        fleet_storage::extend_blob(&mut storage, &budget, &cap, &mut system, b, 2, ts::ctx(&mut s));
        ts::return_shared(storage); ts::return_shared(budget); ts::return_to_address(OWNER, cap);
        prune(&mut s, 5, vector[0, 1]); finish(s, system);
    }
    fun second_blob(s: &ts::Scenario): ID {
        let writer = ts::take_shared<DeviceData>(s);
        let id = fleet_data::anchored_blob(&writer, 1);
        ts::return_shared(writer); id
    }
    #[test, expected_failure(abort_code = fleet_data::ENotFound)]
    fun prune_twice_is_refused() {
        let (mut s, mut system) = setup(); archives(&mut s, &mut system);
        prune(&mut s, 5, vector[0]); prune(&mut s, 5, vector[0]); finish(s, system);
    }
    #[test, expected_failure(abort_code = fleet_data::EBounds)]
    fun prune_requires_indices() {
        let (mut s, system) = setup(); prune(&mut s, 5, vector[]); finish(s, system);
    }
    #[test, expected_failure(abort_code = sui_edge::fleet_budget::ENotOwner)]
    fun prune_requires_the_fleet_owner_cap() {
        let (mut s, mut system) = setup(); archives(&mut s, &mut system);
        ts::next_tx(&mut s, OWNER);
        let registry = ts::take_shared<DeviceRegistry>(&s);
        let other = fleet_budget::create(&registry, coin::mint_for_testing<SUI>(1_000, ts::ctx(&mut s)),
            fleet_budget::new_limits(10, DAY, 10, 1, 0), 0, ts::ctx(&mut s));
        ts::return_shared(registry);
        let fleet = ts::take_shared<FleetData>(&s);
        let mut writer = ts::take_shared<DeviceData>(&s);
        let mut storage = ts::take_shared<FleetStorage>(&s);
        let budget = ts::take_shared<FleetBudget>(&s);
        fleet_data::prune_at_epoch_for_testing(&fleet, &mut writer, &budget, &other, &mut storage, 5, vector[0]);
        abort 0
    }
    #[test]
    fun released_blob_anchor_prunes_only_after_its_recorded_expiry() {
        let (mut s, mut system) = setup(); let (a, _) = archives(&mut s, &mut system);
        let released = release(&mut s, a);
        prune(&mut s, 5, vector[0]);
        let (count, blobs, first, _) = counts(&mut s);
        assert!(count == 2 && blobs == 1 && !first);
        destroy(released); finish(s, system);
    }
    #[test, expected_failure(abort_code = fleet_data::EUnexpired)]
    fun released_blob_anchor_is_kept_until_expiry() {
        let (mut s, mut system) = setup(); let (a, _) = archives(&mut s, &mut system);
        let released = release(&mut s, a);
        prune(&mut s, 4, vector[0]);
        destroy(released); finish(s, system);
    }
    #[test]
    fun burn_expired_unanchored_blob_then_prune_its_anchor() {
        let (mut s, mut system) = setup();
        let (a, _) = archives(&mut s, &mut system);
        let loose = upload(&mut s, &mut system, 50, false);
        burn(&mut s, 5, vector[loose, a]);
        let (_, blobs, first, _) = counts(&mut s);
        assert!(blobs == 1 && first);
        // The anchor of a burned blob is then pruned without another burn.
        prune(&mut s, 5, vector[0]);
        let (_, blobs, first, _) = counts(&mut s);
        assert!(blobs == 1 && !first);
        finish(s, system);
    }
    #[test, expected_failure(abort_code = fleet_data::EUnexpired)]
    fun burn_refuses_unexpired_blob() {
        let (mut s, mut system) = setup(); let loose = upload(&mut s, &mut system, 50, false);
        burn(&mut s, 4, vector[loose]); finish(s, system);
    }
    #[test, expected_failure(abort_code = fleet_data::ENotFound)]
    fun burn_refuses_unknown_blob() {
        let (mut s, mut system) = setup(); let loose = upload(&mut s, &mut system, 50, false);
        burn(&mut s, 5, vector[loose, loose]); finish(s, system);
    }
    #[test, expected_failure]
    fun pruned_blob_cannot_be_anchored_again() {
        let (mut s, mut system) = setup(); let (a, _) = archives(&mut s, &mut system);
        prune(&mut s, 5, vector[0]);
        anchor(&mut s, a, 3, 0, 2 * DAY + 1); finish(s, system);
    }
    fun scoped(grant: bool, device_wide: bool, wrong_stream: bool, revoke: bool, legacy: bool) {
        let (mut s,system)=setup();
        ts::next_tx(&mut s,OWNER);
        let mut fleet=ts::take_shared<FleetData>(&s);
        let budget=ts::take_shared<FleetBudget>(&s);
        let account=ts::take_shared<GasAccount>(&s);
        let device_id=fleet_budget::account_device_id(&account);
        let cap=ts::take_from_address<BudgetOwnerCap>(&s,OWNER);
        let stream=std::hash::sha2_256(b"sensor");
        if (grant) fleet_data::set_read_grant(&mut fleet,&budget,&cap,device_id,if(device_wide)vector[] else stream,SUPPORT,true);
        if (revoke) fleet_data::set_read_grant(&mut fleet,&budget,&cap,device_id,if(device_wide)vector[] else stream,SUPPORT,false);
        let mut id=fleet_data::identity_prefix(&fleet);id.append(device_id.to_bytes());id.append(stream);id.append(zeros(16));
        assert!(id.length()==120 && !fleet_data::identity_matches(&fleet,&id),100);
        assert!(fleet_data::scoped_identity_matches(&fleet,&id,device_id,&stream),100);
        assert!(!fleet_data::scoped_identity_matches(&fleet,&id,device_id,&zeros(32)),100);
        ts::return_shared(fleet);ts::return_shared(account);ts::return_shared(budget);ts::return_to_address(OWNER,cap);
        ts::next_tx(&mut s,SUPPORT);
        let fleet=ts::take_shared<FleetData>(&s);
        if (legacy) fleet_data::approve_support_for_testing(id,&fleet,ts::ctx(&mut s))
        else fleet_data::approve_scoped_for_testing(id,&fleet,device_id,if(wrong_stream)zeros(32) else stream,ts::ctx(&mut s));
        ts::return_shared(fleet);destroy(system);ts::end(s);
    }
    #[test] fun scoped_stream_grant() {scoped(true,false,false,false,false)}
    #[test] fun scoped_device_grant() {scoped(true,true,false,false,false)}
    #[test, expected_failure(abort_code=fleet_data::EAccess)]
    fun scoped_wrong_stream() {scoped(true,false,true,false,false)}
    #[test, expected_failure(abort_code=fleet_data::EAccess)]
    fun scoped_revoked_stream() {scoped(true,false,false,true,false)}
    #[test, expected_failure(abort_code=fleet_data::EAccess)]
    fun scoped_no_grant() {scoped(false,false,false,false,false)}
    #[test, expected_failure(abort_code=fleet_data::EAccess)]
    fun legacy_approval_refuses_scoped_identity() {scoped(true,true,false,false,true)}

    public struct ItemWire has drop {
        sequence:u64,stream:vector<u8>,key:vector<u8>,content_type:vector<u8>,metadata:vector<u8>,upload:vector<u8>,
        chunk:u32,chunks:u32,total_size:u64,size:u64,sha256:vector<u8>,identifier:vector<u8>,ciphertext_size:u64,ciphertext_sha256:vector<u8>,
    }
    #[test]
    fun opaque_items_share_one_certified_anchor() {
        let (mut s,mut system)=setup();
        let blob=upload(&mut s,&mut system,123,true);
        ts::next_tx(&mut s,DEVICE);
        let fleet=ts::take_shared<FleetData>(&s);let mut writer=ts::take_shared<DeviceData>(&s);
        let budget=ts::take_shared<FleetBudget>(&s);let account=ts::take_shared<GasAccount>(&s);
        let registry=ts::take_shared<DeviceRegistry>(&s);let storage=ts::take_shared<FleetStorage>(&s);
        let cap=ts::take_from_address<DeviceCap>(&s,DEVICE);let clock=clock_at(&mut s,DAY);
        let first=ItemWire {sequence:1,stream:b"sensor",key:b"raw",content_type:b"application/octet-stream",metadata:b"{}",upload:zeros(64),chunk:0,chunks:1,total_size:3,size:3,sha256:zeros(32),identifier:b"data-00",ciphertext_size:100,ciphertext_sha256:zeros(32)};
        let second=ItemWire {sequence:2,stream:b"text",key:b"reading",content_type:b"text/plain",metadata:b"{}",upload:zeros(64),chunk:0,chunks:1,total_size:5,size:5,sha256:zeros(32),identifier:b"data-01",ciphertext_size:110,ciphertext_sha256:zeros(32)};
        fleet_data::anchor_items(&fleet,&mut writer,&budget,&account,&registry,&cap,&storage,blob,1,0,zeros(32),vector[sui::bcs::to_bytes(&first),sui::bcs::to_bytes(&second)],&clock,ts::ctx(&mut s));
        assert!(fleet_data::anchor_count(&writer)==1,100);
        assert!(fleet_data::item_exists_for_testing(&writer,1,0) && fleet_data::item_exists_for_testing(&writer,1,1),100);
        clock::destroy_for_testing(clock);ts::return_shared(fleet);ts::return_shared(writer);ts::return_shared(storage);ts::return_shared(budget);
        ts::return_shared(account);ts::return_shared(registry);ts::return_to_address(DEVICE,cap);destroy(system);ts::end(s);
    }

}
