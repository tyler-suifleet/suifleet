#[test_only]
module fleet_data::fleet_data_tests {
    use std::string;
    use std::unit_test::destroy;
    use sui::clock::{Self, Clock};
    use sui::coin;
    use sui::sui::SUI;
    use sui::test_scenario::{Self as ts};
    use sui_edge::customer::{Self, Customer, Fleet as ManagedFleet};
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

    fun managed_setup(): (ts::Scenario, System, ID, ID) {
        let (mut s, system) = setup();
        s.next_tx(OWNER);
        let cap = s.take_from_sender<BudgetOwnerCap>(); let bid = fleet_budget::budget_id(&cap);
        transfer::public_transfer(cap, OWNER);
        let mut r = s.take_shared<DeviceRegistry>(); customer::create(&mut r, string::utf8(b"Customer"), s.ctx());
        ts::return_shared(r); s.next_tx(OWNER);
        let mut c = s.take_shared<Customer>(); let r = s.take_shared<DeviceRegistry>();
        customer::set_entitlement(&mut c, &r, 1, 10000000, 1, s.ctx());
        let mut b = s.take_shared_by_id<FleetBudget>(bid); let cap = s.take_from_sender<BudgetOwnerCap>();
        customer::adopt_budget(&mut c, &mut b, cap, string::utf8(b"Existing"), s.ctx());
        ts::return_shared(c); ts::return_shared(r); ts::return_shared(b); s.next_tx(OWNER);
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared<ManagedFleet>(); let fid = object::id(&f);
        let b = s.take_shared_by_id<FleetBudget>(bid); let mut r = s.take_shared<DeviceRegistry>();
        let mut a = s.take_shared<GasAccount>(); let cap = s.take_from_sender<DeviceOwnerCap>();
        customer::adopt_device(&mut c, &mut f, &b, &mut a, &mut r, cap, s.ctx());
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(b); ts::return_shared(r); ts::return_shared(a);
        (s, system, fid, bid)
    }
    #[test]
    fun customer_approval_preserves_original_archive_identity_after_adoption() {
        let (mut s, system, fid, bid) = managed_setup(); s.next_tx(OWNER);
        let fleet = s.take_shared<FleetData>(); let c = s.take_shared<Customer>(); let f = s.take_shared_by_id<ManagedFleet>(fid);
        let b = s.take_shared_by_id<FleetBudget>(bid); let mut id = fleet_data::identity_prefix(&fleet); id.append(zeros(16));
        fleet_data::approve_customer_for_testing(id, &fleet, &b, &c, &f, s.ctx());
        ts::return_shared(fleet); ts::return_shared(c); ts::return_shared(f); ts::return_shared(b); finish(s, system);
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::customer)]
    fun customer_membership_does_not_give_read_only_credentials_archive_keys() {
        let (mut s, system, fid, bid) = managed_setup(); s.next_tx(OWNER);
        let mut c = s.take_shared<Customer>(); customer::set_credential(&mut c, SUPPORT, true, false, s.ctx());
        ts::return_shared(c); s.next_tx(SUPPORT);
        let fleet = s.take_shared<FleetData>(); let c = s.take_shared<Customer>(); let f = s.take_shared_by_id<ManagedFleet>(fid);
        let b = s.take_shared_by_id<FleetBudget>(bid); let mut id = fleet_data::identity_prefix(&fleet); id.append(zeros(16));
        fleet_data::approve_customer_for_testing(id, &fleet, &b, &c, &f, s.ctx());
        ts::return_shared(fleet); ts::return_shared(c); ts::return_shared(f); ts::return_shared(b); finish(s, system);
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
