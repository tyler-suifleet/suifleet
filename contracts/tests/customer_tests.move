#[test_only]
module sui_edge::customer_tests {
    use std::string::utf8;
    use sui::coin;
    use sui::sui::SUI;
    use sui::test_scenario::{Self as ts};
    use sui_edge::customer::{Self, Customer, Fleet};
    use sui_edge::device_registry::{Self, DeviceRegistry};
    use sui_edge::fleet_budget::{Self, FleetBudget};

    const OWNER: address = @0xA;
    const OTHER: address = @0xB;

    fun setup(): ts::Scenario {
        let mut s = ts::begin(OWNER);
        device_registry::init_for_testing(s.ctx());
        s.next_tx(OWNER);
        let mut registry = s.take_shared<DeviceRegistry>();
        customer::create(&mut registry, utf8(b"Customer"), s.ctx());
        // Retry is a no-op, not a second customer.
        customer::create(&mut registry, utf8(b"Customer"), s.ctx());
        ts::return_shared(registry);
        s.next_tx(OWNER);
        s
    }

    fun fleet(s: &mut ts::Scenario): ID {
        let registry = s.take_shared<DeviceRegistry>();
        let cap = fleet_budget::create(&registry, coin::mint_for_testing<SUI>(0, s.ctx()),
            fleet_budget::new_limits(1, 1000, 1, 1, 1), 0, s.ctx());
        transfer::public_transfer(cap, OWNER);
        ts::return_shared(registry);
        s.next_tx(OWNER);
        let mut budget = s.take_shared_by_id<FleetBudget>(ts::most_recent_id_shared<FleetBudget>().destroy_some());
        let cap = s.take_from_sender<sui_edge::fleet_budget::BudgetOwnerCap>();
        let mut c = s.take_shared<Customer>();
        customer::adopt_budget(&mut c, &mut budget, cap, utf8(b"Production"), s.ctx());
        ts::return_shared(c);
        ts::return_shared(budget);
        s.next_tx(OWNER);
        ts::most_recent_id_shared<Fleet>().destroy_some()
    }

    #[test]
    fun default_retry_and_two_fleets_with_identical_aliases() {
        let mut s = setup();
        let first = fleet(&mut s);
        let second = fleet(&mut s);
        assert!(first != second);
        let c = s.take_shared<Customer>();
        assert!(customer::fleets(&c).length() == 3);
        assert!(customer::active_devices(&c) == 0);
        ts::return_shared(c);
        s.end();
    }

    #[test]
    fun credential_rotation_preserves_allowance() {
        let mut s = setup();
        let fid = fleet(&mut s);
        let mut c = s.take_shared<Customer>();
        let registry = s.take_shared<DeviceRegistry>();
        customer::set_entitlement(&mut c, &registry, 2, 10, 1, s.ctx());
        let f = s.take_shared_by_id<Fleet>(fid);
        customer::reserve_allowance(&mut c, &f, 6, s.ctx());
        customer::set_credential(&mut c, OTHER, true, true, s.ctx());
        ts::return_shared(registry); ts::return_shared(f); ts::return_shared(c);
        s.next_tx(OTHER);
        let mut c = s.take_shared<Customer>();
        customer::set_credential(&mut c, OWNER, false, false, s.ctx());
        customer::assert_admin(&c, s.ctx());
        assert!(customer::used(&c) == 6);
        ts::return_shared(c); s.end();
    }

    #[test, expected_failure(abort_code = 0, location = sui_edge::customer)]
    fun revoked_wallet_cannot_reuse_membership() {
        let mut s = setup();
        let mut c = s.take_shared<Customer>();
        customer::set_credential(&mut c, OTHER, true, true, s.ctx());
        customer::set_credential(&mut c, OTHER, false, false, s.ctx());
        ts::return_shared(c); s.next_tx(OTHER);
        let c = s.take_shared<Customer>();
        customer::assert_admin(&c, s.ctx());
        ts::return_shared(c); s.end();
    }

    #[test, expected_failure(abort_code = 4, location = sui_edge::customer)]
    fun last_administrator_is_preserved() {
        let mut s = setup(); let mut c = s.take_shared<Customer>();
        customer::set_credential(&mut c, OWNER, false, false, s.ctx());
        ts::return_shared(c); s.end();
    }

    #[test, expected_failure(abort_code = 2, location = sui_edge::customer)]
    fun fleet_creation_does_not_reset_customer_allowance() {
        let mut s = setup(); let first = fleet(&mut s); let second = fleet(&mut s);
        let mut c = s.take_shared<Customer>(); let registry = s.take_shared<DeviceRegistry>();
        customer::set_entitlement(&mut c, &registry, 2, 10, 1, s.ctx());
        let a = s.take_shared_by_id<Fleet>(first); let b = s.take_shared_by_id<Fleet>(second);
        customer::reserve_allowance(&mut c, &a, 7, s.ctx());
        customer::reserve_allowance(&mut c, &b, 4, s.ctx());
        ts::return_shared(a); ts::return_shared(b); ts::return_shared(registry); ts::return_shared(c); s.end();
    }
    fun allow_devices(s: &mut ts::Scenario, count: u64) {
        let mut c = s.take_shared<Customer>(); let r = s.take_shared<DeviceRegistry>();
        customer::set_entitlement(&mut c, &r, count, 100000, 1, s.ctx());
        ts::return_shared(c); ts::return_shared(r);
        s.next_tx(OWNER);
    }
    fun register(s: &mut ts::Scenario, fid: ID, address: address): ID { register_arch(s, fid, address, b"x86_64") }
    fun register_arch(s: &mut ts::Scenario, fid: ID, address: address, arch: vector<u8>): ID {
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut r = s.take_shared<DeviceRegistry>();
        let mut b = s.take_shared_by_id<FleetBudget>(customer::fleet_budget(&f));
        let clock = sui::clock::create_for_testing(s.ctx());
        customer::register_device(&mut c, &mut f, &mut b, &mut r, utf8(b"Device"), address, utf8(arch), &clock, s.ctx());
        let ids = customer::devices(&f);
        let did = ids[ids.length() - 1];
        sui::clock::destroy_for_testing(clock);
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(b); ts::return_shared(r);
        did
    }
    #[test, expected_failure(abort_code = 2, location = sui_edge::customer)]
    fun zero_limit_refuses_first_device() {
        let mut s = setup(); let f = fleet(&mut s);
        register(&mut s, f, @0xD1); s.end();
    }
    #[test]
    fun registry_grant_unblocks_zero_limit_and_can_raise_boundary() {
        let mut s = setup(); let f = fleet(&mut s);
        allow_devices(&mut s, 1); register(&mut s, f, @0xD1); s.next_tx(OWNER);
        allow_devices(&mut s, 2); register(&mut s, f, @0xD2); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); assert!(customer::active_devices(&c) == 2);
        ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::device_registry)]
    fun customer_admin_cannot_grant_registry_entitlement() {
        let mut s = setup(); let mut c = s.take_shared<Customer>();
        customer::set_credential(&mut c, OTHER, true, true, s.ctx()); ts::return_shared(c);
        s.next_tx(OTHER);
        let mut c = s.take_shared<Customer>(); let r = s.take_shared<DeviceRegistry>();
        customer::set_entitlement(&mut c, &r, 3, 0, 0, s.ctx());
        ts::return_shared(c); ts::return_shared(r); s.end();
    }
    #[test]
    fun two_fleets_share_device_accounting_and_budget_custody() {
        let mut s = setup(); let a = fleet(&mut s); let b = fleet(&mut s);
        allow_devices(&mut s, 2);
        let da = register(&mut s, a, @0xD1); s.next_tx(OWNER);
        let db = register(&mut s, b, @0xD2); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); assert!(customer::active_devices(&c) == 2);
        let mut f = s.take_shared_by_id<Fleet>(a);
        assert!(customer::devices(&f).contains(&da) && !customer::devices(&f).contains(&db));
        let (cap, loan) = customer::borrow_budget_cap(&c, &mut f, s.ctx());
        let mut budget = s.take_shared_by_id<FleetBudget>(customer::fleet_budget(&f));
        fleet_budget::set_paused(&mut budget, &cap, true);
        customer::return_budget_cap(&mut f, cap, loan);
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(budget); s.end();
    }
    #[test, expected_failure(abort_code = 2, location = sui_edge::customer)]
    fun another_fleet_cannot_bypass_device_limit() {
        let mut s = setup(); let a = fleet(&mut s); let b = fleet(&mut s);
        allow_devices(&mut s, 1); register(&mut s, a, @0xD1); s.next_tx(OWNER);
        register(&mut s, b, @0xD2); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun same_wallet_cannot_add_foreign_fleet_device_to_group() {
        let mut s = setup(); let a = fleet(&mut s); let b = fleet(&mut s);
        allow_devices(&mut s, 2);
        let da = register(&mut s, a, @0xD1); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(b);
        let clock = sui::clock::create_for_testing(s.ctx());
        customer::create_group(&c, &mut f, utf8(b"Group"), utf8(b""), &clock, s.ctx());
        sui::clock::destroy_for_testing(clock); ts::return_shared(c); ts::return_shared(f);
        s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(b);
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        customer::add_to_group(&c, &mut f, &mut g, da, s.ctx());
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(g); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun cross_fleet_deployment_targets_are_rejected() {
        let mut s = setup(); let a = fleet(&mut s); let b = fleet(&mut s);
        allow_devices(&mut s, 2); let da = register(&mut s, a, @0xD1); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(b);
        let clock = sui::clock::create_for_testing(s.ctx());
        customer::create_deployment(&c, &mut f, utf8(b"app"), utf8(b"1"), utf8(b"blob"), utf8(b"hash"),
            utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), vector[], vector[da], &clock, s.ctx());
        sui::clock::destroy_for_testing(clock); ts::return_shared(c); ts::return_shared(f); s.end();
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::device_group)]
    fun legacy_group_entry_point_cannot_write_managed_group() {
        let mut s = setup(); let a = fleet(&mut s);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(a);
        let clock = sui::clock::create_for_testing(s.ctx());
        customer::create_group(&c, &mut f, utf8(b"Group"), utf8(b""), &clock, s.ctx());
        sui::clock::destroy_for_testing(clock); ts::return_shared(c); ts::return_shared(f); s.next_tx(OWNER);
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        sui_edge::device_group::remove_device(&mut g, a, s.ctx());
        ts::return_shared(g); s.end();
    }

    fun claim_setup(): (ts::Scenario, ID) {
        let mut s = setup(); let fid = fleet(&mut s); allow_devices(&mut s, 1);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid); let r = s.take_shared<DeviceRegistry>();
        customer::create_enrollment_policy(&c, &mut f, &r, 2, 0, s.ctx());
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(r); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let f = s.take_shared_by_id<Fleet>(fid);
        let mut p = s.take_shared<sui_edge::enrollment::EnrollmentPolicy>();
        customer::add_claims(&c, &f, &mut p, vector[@0xCA], s.ctx());
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(p);
        (s, fid)
    }
    #[test]
    fun sponsored_managed_claim_consumes_customer_capacity_without_wallet_membership() {
        let (mut s, fid) = claim_setup(); s.next_tx(@0xD1);
        let builder = ts::ctx_builder(&s).set_sponsor(@0xCA); ts::next_with_context(&mut s, builder);
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut p = s.take_shared<sui_edge::enrollment::EnrollmentPolicy>();
        let mut b = s.take_shared_by_id<FleetBudget>(customer::fleet_budget(&f)); let mut r = s.take_shared<DeviceRegistry>();
        let clock = sui::clock::create_for_testing(s.ctx());
        customer::enroll_claim(&mut c, &mut f, &mut b, &mut p, &mut r, utf8(b"Claimed"), utf8(b"x86_64"), &clock, s.ctx());
        assert!(customer::active_devices(&c) == 1 && customer::devices(&f).length() == 1);
        assert!(sui_edge::enrollment::remaining(&p) == 1 && !sui_edge::enrollment::is_claim(&p, @0xCA));
        sui::clock::destroy_for_testing(clock); ts::return_shared(c); ts::return_shared(f); ts::return_shared(p); ts::return_shared(b); ts::return_shared(r); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::enrollment)]
    fun old_enrollment_entry_is_sealed_by_policy_open_flag() {
        let (mut s, _) = claim_setup(); s.next_tx(@0xD1);
        let builder = ts::ctx_builder(&s).set_sponsor(@0xCA); ts::next_with_context(&mut s, builder);
        let mut p = s.take_shared<sui_edge::enrollment::EnrollmentPolicy>(); let mut r = s.take_shared<DeviceRegistry>();
        let clock = sui::clock::create_for_testing(s.ctx());
        sui_edge::enrollment::enroll(&mut p, &mut r, utf8(b"Bypass"), utf8(b"x86_64"), &clock, s.ctx());
        sui::clock::destroy_for_testing(clock); ts::return_shared(p); ts::return_shared(r); s.end();
    }
    #[test]
    fun removal_returns_custody_and_releases_aggregate_capacity() {
        let mut s = setup(); let a = fleet(&mut s); allow_devices(&mut s, 1);
        let did = register(&mut s, a, @0xD1); s.next_tx(OWNER);
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(a);
        let mut b = s.take_shared_by_id<FleetBudget>(customer::fleet_budget(&f));
        let mut account = s.take_shared<sui_edge::fleet_budget::GasAccount>();
        let cap = customer::remove_device(&mut c, &mut f, &mut b, &mut account, did, s.ctx());
        assert!(device_registry::owner_cap_device_id(&cap) == did);
        assert!(customer::active_devices(&c) == 0 && customer::devices(&f).is_empty());
        assert!(fleet_budget::device_count(&b) == 0);
        // The PTB can explicitly choose custody after the authoritative release.
        sui::transfer::public_transfer(cap, OWNER);
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(b); ts::return_shared(account);
        s.next_tx(OWNER);
        let cap = s.take_from_sender<sui_edge::device_registry::DeviceOwnerCap>();
        assert!(device_registry::owner_cap_device_id(&cap) == did);
        ts::return_to_address(OWNER, cap); s.end();
    }
    #[test]
    fun explicit_reassignment_keeps_identity_and_aggregate_count() {
        let mut s = setup(); let a = fleet(&mut s); let b = fleet(&mut s); allow_devices(&mut s, 1);
        let did = register(&mut s, a, @0xD1); s.next_tx(OWNER);
        let mut c = s.take_shared<Customer>(); let mut source = s.take_shared_by_id<Fleet>(a); let mut dest = s.take_shared_by_id<Fleet>(b);
        let mut sb = s.take_shared_by_id<FleetBudget>(customer::fleet_budget(&source));
        let mut db = s.take_shared_by_id<FleetBudget>(customer::fleet_budget(&dest));
        let mut account = s.take_shared<sui_edge::fleet_budget::GasAccount>(); let r = s.take_shared<DeviceRegistry>();
        let before = device_registry::identity(&r, did).destroy_some();
        let clock = sui::clock::create_for_testing(s.ctx());
        customer::reassign_device(&mut c, &mut source, &mut dest, &mut sb, &mut db, &mut account, &r, did, &clock, s.ctx());
        assert!(customer::active_devices(&c) == 1 && customer::devices(&source).is_empty() && customer::devices(&dest).contains(&did));
        assert!(fleet_budget::device_count(&sb) == 0 && fleet_budget::device_count(&db) == 1);
        let after = device_registry::identity(&r, did).destroy_some();
        assert!(device_registry::identity_owner_cap(&before) == device_registry::identity_owner_cap(&after));
        assert!(device_registry::identity_signer(&before) == device_registry::identity_signer(&after));
        sui::clock::destroy_for_testing(clock); ts::return_shared(c); ts::return_shared(source); ts::return_shared(dest);
        ts::return_shared(sb); ts::return_shared(db); ts::return_shared(account); ts::return_shared(r); s.end();
    }
    #[test, expected_failure(abort_code = 5, location = sui_edge::deployment_manager)]
    fun legacy_receipt_api_cannot_report_for_managed_deployment() {
        let mut s = setup(); let a = fleet(&mut s); allow_devices(&mut s, 1); let did = register(&mut s, a, @0xD1); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(a); let clock = sui::clock::create_for_testing(s.ctx());
        customer::create_deployment(&c, &mut f, utf8(b"app"), utf8(b"1"), utf8(b"blob"), utf8(b"hash"),
            utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), vector[], vector[did], &clock, s.ctx());
        sui::clock::destroy_for_testing(clock); ts::return_shared(c); ts::return_shared(f); s.next_tx(@0xD1);
        let mut d = s.take_shared<sui_edge::deployment_manager::DeploymentRecord>(); let r = s.take_shared<DeviceRegistry>();
        let cap = s.take_from_sender<sui_edge::device_cap::DeviceCap>();
        sui_edge::deployment_manager::report_verified_status(&mut d, &r, &cap, 1, s.ctx());
        ts::return_shared(d); ts::return_shared(r); ts::return_to_address(@0xD1, cap); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun policy_device_grants_cannot_cross_fleets() {
        let mut s = setup(); let a = fleet(&mut s); let b = fleet(&mut s); allow_devices(&mut s, 1); let did = register(&mut s, a, @0xD1); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(b);
        customer::create_policy(&c, &mut f, utf8(b"Private release"), s.ctx()); ts::return_shared(c); ts::return_shared(f); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(b);
        let mut p = s.take_shared<sui_edge::seal_policy::AccessPolicy>(); let r = s.take_shared<DeviceRegistry>();
        customer::policy_device(&c, &mut f, &mut p, &r, did, true, s.ctx());
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(p); ts::return_shared(r); s.end();
    }

    #[test, expected_failure(abort_code = 0, location = sui_edge::customer)]
    fun future_customer_authority_is_not_writable_through_current_credential_api() {
        let mut s = setup(); let fid = fleet(&mut s);
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        customer::set_authority_version_for_testing(&mut c, &mut f, 2);
        customer::set_credential(&mut c, OTHER, true, true, s.ctx());
        ts::return_shared(c); ts::return_shared(f); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun future_customer_authority_cannot_reset_entitlements_through_current_code() {
        let mut s = setup(); let fid = fleet(&mut s);
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let r = s.take_shared<DeviceRegistry>();
        customer::set_authority_version_for_testing(&mut c, &mut f, 2);
        customer::set_entitlement(&mut c, &r, 999, 999, 2, s.ctx());
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(r); s.end();
    }

    // mode 0: deregistered; 1: adopted in another budget; 2: unavailable cap;
    // mode 3: live adoptable account (explicit administrator retirement allowed).
    fun pending_fixture(mode: u8): (ts::Scenario, ID, ID, ID) {
        let mut s = setup(); allow_devices(&mut s, 3);
        let mut r = s.take_shared<DeviceRegistry>();
        let clock = sui::clock::create_for_testing(s.ctx());
        let device = device_registry::register_device(&mut r, utf8(b"Legacy"), @0xD1, utf8(b"x86_64"), &clock, s.ctx());
        let cap = device_registry::activate_device(&mut r, device, s.ctx());
        let did = device_registry::owner_cap_device_id(&cap);
        let (mut budget, bcap) = fleet_budget::new_budget(&r, coin::mint_for_testing<SUI>(100, s.ctx()),
            fleet_budget::new_limits(1, 1000, 1, 1, 1), 0, s.ctx());
        let bid = object::id(&budget);
        let aid = fleet_budget::enroll_device(&mut budget, &bcap, &r, &cap, 0, 10, &clock, s.ctx());
        let mut c = s.take_shared<Customer>();
        if (mode == 1) {
            let (mut other, ocap) = fleet_budget::new_budget(&r, coin::zero(s.ctx()),
                fleet_budget::new_limits(1, 1000, 1, 1, 1), 0, s.ctx());
            fleet_budget::enroll_device(&mut other, &ocap, &r, &cap, 0, 0, &clock, s.ctx());
            customer::adopt_budget(&mut c, &mut other, ocap, utf8(b"Other"), s.ctx());
            fleet_budget::share_managed(other);
        };
        customer::adopt_budget(&mut c, &mut budget, bcap, utf8(b"Legacy"), s.ctx());
        fleet_budget::share_managed(budget);
        if (mode == 0) device_registry::deregister_activated_device(&mut r, cap, s.ctx())
        else transfer::public_transfer(cap, if (mode == 2) OTHER else OWNER);
        sui::clock::destroy_for_testing(clock); ts::return_shared(c); ts::return_shared(r);
        s.next_tx(OWNER);
        let fid = ts::most_recent_id_shared<Fleet>().destroy_some();
        if (mode == 1) {
            let mut c = s.take_shared<Customer>(); let ids = customer::fleets(&c);
            let other_id = ids[ids.length() - 2];
            let mut f = s.take_shared_by_id<Fleet>(other_id);
            let b = s.take_shared_by_id<FleetBudget>(customer::fleet_budget(&f));
            let mut account = s.take_shared_by_id<sui_edge::fleet_budget::GasAccount>(fleet_budget::account_id(&b, did).destroy_some());
            let mut r = s.take_shared<DeviceRegistry>(); let cap = s.take_from_sender<sui_edge::device_registry::DeviceOwnerCap>();
            customer::adopt_device(&mut c, &mut f, &b, &mut account, &mut r, cap, s.ctx());
            ts::return_shared(c); ts::return_shared(f); ts::return_shared(b); ts::return_shared(account); ts::return_shared(r);
            s.next_tx(OWNER);
        };
        (s, fid, bid, aid)
    }
    fun retire_fixture(mode: u8, twice: bool, unauthorized: bool) {
        let (mut s, fid, bid, aid) = pending_fixture(mode);
        if (unauthorized) { s.next_tx(OTHER); };
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let before = customer::active_devices(&c);
        let mut b = s.take_shared_by_id<FleetBudget>(bid);
        let mut a = s.take_shared_by_id<sui_edge::fleet_budget::GasAccount>(aid);
        customer::retire_pending_device(&mut c, &mut f, &mut b, &mut a, s.ctx());
        assert!(customer::active_devices(&c) == before - 1 && fleet_budget::device_count(&b) == 0);
        assert!(fleet_budget::account_closed(&a) && fleet_budget::account_reserve(&a) == 0);
        assert!(fleet_budget::account_id(&b, fleet_budget::account_device_id(&a)).is_none());
        if (twice) customer::retire_pending_device(&mut c, &mut f, &mut b, &mut a, s.ctx());
        if (mode == 1) {
            let ids = customer::fleets(&c);
            let other = s.take_shared_by_id<Fleet>(ids[ids.length() - 2]);
            let did = fleet_budget::account_device_id(&a);
            assert!(customer::devices(&other).contains(&did) && customer::active_devices(&c) == 1);
            let r = s.take_shared<DeviceRegistry>();
            assert!(device_registry::identity(&r, did).is_some());
            ts::return_shared(other); ts::return_shared(r);
        };
        // Completion unlocks the exact cap, funds and settings, not just a counter.
        let (cap, loan) = customer::borrow_budget_cap(&c, &mut f, s.ctx());
        let funds = fleet_budget::withdraw(&mut b, &cap, 100, s.ctx());
        coin::burn_for_testing(funds);
        customer::return_budget_cap(&mut f, cap, loan);
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(b); ts::return_shared(a); s.end();
    }
    #[test] fun retire_deregistered_legacy_account_unlocks_funds() { retire_fixture(0, false, false); }
    #[test] fun retire_duplicate_legacy_account_preserves_other_assignment() { retire_fixture(1, false, false); }
    #[test] fun retire_lost_cap_account_unlocks_funds() { retire_fixture(2, false, false); }
    #[test] fun admin_can_explicitly_retire_live_pending_account() { retire_fixture(3, false, false); }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun pending_account_cannot_be_retired_twice() { retire_fixture(0, true, false); }
    #[test, expected_failure(abort_code = 0, location = sui_edge::customer)]
    fun non_admin_cannot_retire_pending_account() { retire_fixture(0, false, true); }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun adopted_device_cannot_be_retired_as_pending() {
        let (mut s, fid, bid, aid) = pending_fixture(3);
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let b = s.take_shared_by_id<FleetBudget>(bid);
        let mut a = s.take_shared_by_id<sui_edge::fleet_budget::GasAccount>(aid);
        let mut r = s.take_shared<DeviceRegistry>();
        let cap = s.take_from_sender<sui_edge::device_registry::DeviceOwnerCap>();
        customer::adopt_device(&mut c, &mut f, &b, &mut a, &mut r, cap, s.ctx());
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(b); ts::return_shared(a); ts::return_shared(r);
        s.next_tx(OWNER);
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut b = s.take_shared_by_id<FleetBudget>(bid);
        let mut a = s.take_shared_by_id<sui_edge::fleet_budget::GasAccount>(aid);
        customer::retire_pending_device(&mut c, &mut f, &mut b, &mut a, s.ctx());
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(b); ts::return_shared(a); s.end();
    }

    fun managed_onchain_tick(foreign: bool, revoked: bool) {
        let mut s = setup(); let a = fleet(&mut s); let b = fleet(&mut s);
        allow_devices(&mut s, 1);
        let device = register(&mut s, a, @0xD1); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(a);
        let mut clock = sui::clock::create_for_testing(s.ctx());
        sui::clock::set_for_testing(&mut clock, 1);
        let policy = sui_edge::deployment_manager::rollout_policy(1, 1, 1, 100, 1, 10000, 2000);
        let record = customer::new_rollout(&c, &mut f, utf8(b"app"), utf8(b"1"), utf8(b"blob"),
            utf8(b"hash"), utf8(b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"),
            vector[device], vector[0], policy, &clock, s.ctx());
        sui_edge::deployment_manager::share_deployment(record);
        ts::return_shared(c); ts::return_shared(f); s.next_tx(OWNER);
        if (revoked) { let mut c = s.take_shared<Customer>();
            customer::set_credential(&mut c, OTHER, true, true, s.ctx());
            customer::set_credential(&mut c, OWNER, false, false, s.ctx()); ts::return_shared(c); s.next_tx(OWNER); };
        let c = s.take_shared<Customer>(); let f = s.take_shared_by_id<Fleet>(if (foreign) b else a);
        let registry = s.take_shared<DeviceRegistry>();
        let mut record = s.take_shared<sui_edge::deployment_manager::DeploymentRecord>();
        customer::advance_rollout_onchain(&c, &f, &mut record, &registry, vector[device], &clock, s.ctx());
        assert!(sui_edge::deployment_manager::rollout_eligible(&record, device));
        // No signed post-apply sample: a second tick cannot manufacture success.
        sui::clock::set_for_testing(&mut clock, 1000);
        customer::advance_rollout_onchain(&c, &f, &mut record, &registry, vector[], &clock, s.ctx());
        let (state, _) = sui_edge::deployment_manager::rollout_progress(&record); assert!(state == 0);
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(registry); ts::return_shared(record);
        sui::clock::destroy_for_testing(clock); s.end();
    }
    #[test]
    fun managed_onchain_rollout_requires_signed_health() { managed_onchain_tick(false, false); }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun managed_onchain_rollout_rejects_foreign_fleet() { managed_onchain_tick(true, false); }
    #[test, expected_failure(abort_code = 0, location = sui_edge::customer)]
    fun managed_onchain_rollout_rejects_revoked_wallet() { managed_onchain_tick(false, true); }

    // SF-125 integration: indexed members must participate in adoption and
    // managed group accounting, including when the old vector is empty.
    fun indexed_group(): (ts::Scenario, ID, ID, ID, ID) {
        let mut s = setup(); let a = fleet(&mut s); let b = fleet(&mut s);
        allow_devices(&mut s, 3);
        let da = register(&mut s, a, @0xD1); s.next_tx(OWNER);
        let db = register(&mut s, a, @0xD2); s.next_tx(OWNER);
        let dc = register(&mut s, b, @0xD3); s.next_tx(OWNER);
        let clock = sui::clock::create_for_testing(s.ctx());
        sui_edge::device_group::create_group(utf8(b"Indexed"), utf8(b""), &clock, s.ctx());
        sui::clock::destroy_for_testing(clock); s.next_tx(OWNER);
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        sui_edge::device_group::legacy_add_for_testing(&mut g, da);
        sui_edge::device_group::legacy_add_for_testing(&mut g, db);
        sui_edge::device_group::index_members(&mut g, 200, s.ctx());
        assert!(sui_edge::device_group::device_ids(&g).is_empty());
        ts::return_shared(g); s.next_tx(OWNER);
        (s, a, da, db, dc)
    }
    #[test]
    fun adopt_indexed_group_preserves_members_and_managed_mutations() {
        let (mut s, fid, da, db, _) = indexed_group();
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        customer::adopt_indexed_group(&c, &mut f, &mut g, vector[da, db], s.ctx());
        assert!(sui_edge::device_group::member_count(&g) == 2);
        customer::remove_from_group(&c, &mut f, &mut g, da, s.ctx());
        assert!(!sui_edge::device_group::contains_device(&g, da));
        customer::add_to_group(&c, &mut f, &mut g, da, s.ctx());
        assert!(sui_edge::device_group::member_count(&g) == 2);
        customer::remove_from_group(&c, &mut f, &mut g, da, s.ctx());
        customer::remove_from_group(&c, &mut f, &mut g, db, s.ctx());
        customer::delete_group(&c, &mut f, g, s.ctx());
        ts::return_shared(c); ts::return_shared(f); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun legacy_adoption_cannot_hide_indexed_members() {
        let (mut s, fid, _, _, _) = indexed_group();
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        customer::adopt_group(&c, &mut f, &mut g, s.ctx());
        ts::return_shared(g); ts::return_shared(c); ts::return_shared(f); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun indexed_adoption_rejects_duplicate_snapshot() {
        let (mut s, fid, da, _, _) = indexed_group();
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        customer::adopt_indexed_group(&c, &mut f, &mut g, vector[da, da], s.ctx());
        ts::return_shared(g); ts::return_shared(c); ts::return_shared(f); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun indexed_adoption_rejects_foreign_member_substitution() {
        let (mut s, fid, da, _, dc) = indexed_group();
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        customer::adopt_indexed_group(&c, &mut f, &mut g, vector[da, dc], s.ctx());
        ts::return_shared(g); ts::return_shared(c); ts::return_shared(f); s.end();
    }

    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun indexed_adoption_rejects_actual_foreign_fleet_member() {
        let (mut s, fid, da, db, dc) = indexed_group();
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        sui_edge::device_group::legacy_add_for_testing(&mut g, dc);
        sui_edge::device_group::index_members(&mut g, 200, s.ctx());
        customer::adopt_indexed_group(&c, &mut f, &mut g, vector[da, db, dc], s.ctx());
        ts::return_shared(g); ts::return_shared(c); ts::return_shared(f); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun indexed_managed_group_cannot_be_deleted_with_members() {
        let (mut s, fid, da, db, _) = indexed_group();
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        customer::adopt_indexed_group(&c, &mut f, &mut g, vector[da, db], s.ctx());
        customer::delete_group(&c, &mut f, g, s.ctx());
        ts::return_shared(c); ts::return_shared(f); s.end();
    }

    // Populated managed v3 -> sharded v4, including barrier and cleanup.
    fun managed_shard_case(attack: u8) {
        use sui_edge::deployment_manager as dm;
        use sui_edge::device_cap::DeviceCap;
        let mut s = setup(); let fid = fleet(&mut s); allow_devices(&mut s, 1);
        let did = register(&mut s, fid, @0xD1); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let clock = sui::clock::create_for_testing(s.ctx());
        customer::create_deployment(&c, &mut f, utf8(b"app"), utf8(b"1"), utf8(b"blob"), utf8(b"hash"),
            utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), vector[], vector[did], &clock, s.ctx());
        sui::clock::destroy_for_testing(clock); ts::return_shared(c); ts::return_shared(f); s.next_tx(@0xD1);
        let c = s.take_shared<Customer>(); let f = s.take_shared_by_id<Fleet>(fid);
        let r = s.take_shared<DeviceRegistry>(); let cap = s.take_from_sender<DeviceCap>();
        let mut d = s.take_shared<dm::DeploymentRecord>();
        customer::report_status(&c, &f, &mut d, &r, &cap, 1, s.ctx());
        assert!(dm::receipts_version(&d) == 3 && dm::receipt_status(&d, did) == option::some(1));
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(r); ts::return_shared(d); ts::return_to_sender(&s, cap);
        s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let f = s.take_shared_by_id<Fleet>(fid); let mut d = s.take_shared<dm::DeploymentRecord>();
        customer::begin_sharded_receipts(&c, &f, &mut d, s.ctx());
        customer::begin_sharded_receipts(&c, &f, &mut d, s.ctx());
        assert!(dm::receipts_version(&d) == 4);
        let roots = *dm::receipt_roots(&d);
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(d); s.next_tx(@0xD1);
        let c = s.take_shared<Customer>(); let f = s.take_shared_by_id<Fleet>(fid); let r = s.take_shared<DeviceRegistry>();
        let cap = s.take_from_sender<DeviceCap>(); let mut d = s.take_shared<dm::DeploymentRecord>();
        if (attack == 1) customer::report_status(&c, &f, &mut d, &r, &cap, 2, s.ctx());
        let mut n = 0;
        while (n < 16) {
            let mut root = s.take_shared_by_id<dm::ReceiptShard>(roots[n]);
            dm::migrate_receipt_chunk(&mut d, &mut root, 1);
            dm::migrate_receipt_chunk(&mut d, &mut root, 1);
            ts::return_shared(root); n = n + 1;
        };
        dm::finish_receipt_migration(&mut d);
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(r); ts::return_shared(d); ts::return_to_sender(&s, cap);
        s.next_tx(@0xD1);
        let c = s.take_shared<Customer>(); let f = s.take_shared_by_id<Fleet>(fid); let r = s.take_shared<DeviceRegistry>();
        let cap = s.take_from_sender<DeviceCap>(); let mut d = s.take_shared<dm::DeploymentRecord>();
        let mut root = s.take_shared_by_id<dm::ReceiptShard>(roots[dm::receipt_shard_index(did)]);
        assert!(dm::receipt_status_v3(&d, &root, did) == option::some(1));
        if (attack == 2) dm::report_status_v3(&d, &mut root, &r, &cap, 2, s.ctx());
        customer::report_status_v4(&c, &f, &d, &mut root, &r, &cap, 2, s.ctx());
        assert!(dm::receipt_status_v3(&d, &root, did) == option::some(2));
        if (attack == 3) customer::report_status(&c, &f, &mut d, &r, &cap, 1, s.ctx());
        ts::return_shared(root); ts::return_shared(c); ts::return_shared(f); ts::return_shared(r); ts::return_shared(d); ts::return_to_sender(&s, cap);
        s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid); let mut d = s.take_shared<dm::DeploymentRecord>();
        customer::begin_receipt_cleanup(&c, &f, &mut d, s.ctx());
        n = 0;
        while (n < 16) {
            let mut root = s.take_shared_by_id<dm::ReceiptShard>(roots[n]);
            dm::cleanup_receipt_chunk(&d, &mut root, 1); dm::retire_receipt_shard(&mut d, root); n = n + 1;
        };
        customer::delete_deployment(&c, &mut f, d, s.ctx());
        ts::return_shared(c); ts::return_shared(f); s.end();
    }
    #[test]
    fun populated_managed_shards_preserve_authority_and_cleanup() { managed_shard_case(0); }
    #[test, expected_failure(abort_code = 5, location = sui_edge::deployment_manager)]
    fun managed_v3_report_cannot_write_frozen_snapshot() { managed_shard_case(1); }
    #[test, expected_failure(abort_code = 5, location = sui_edge::deployment_manager)]
    fun unmanaged_shard_route_cannot_bypass_customer() { managed_shard_case(2); }
    #[test, expected_failure(abort_code = 5, location = sui_edge::deployment_manager)]
    fun managed_v3_report_cannot_write_finished_shards() { managed_shard_case(3); }

    #[test]
    fun managed_catalog_desired_revision() {
        use sui_edge::deployment_manager;
        use sui_edge::component_catalog::{Self as cc, ComponentCatalog, ComponentVersion};
        use sui_edge::desired_state;
        use sui_edge::device_group::DeviceGroup;
        let mut s = setup(); let fid = fleet(&mut s); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let clock = sui::clock::create_for_testing(s.ctx());
        customer::create_group(&c, &mut f, utf8(b"Library group"), utf8(b""), &clock, s.ctx());
        cc::create_catalog(s.ctx());
        sui::clock::destroy_for_testing(clock); ts::return_shared(c); ts::return_shared(f); s.next_tx(OWNER);
        let mut catalog = s.take_shared<ComponentCatalog>();
        let clock = sui::clock::create_for_testing(s.ctx());
        cc::publish_version(&mut catalog, utf8(b"app"), utf8(b"1.0.0"),
            vector[utf8(b"linux"), utf8(b"linux")], vector[utf8(b"arm"), utf8(b"x86_64")],
            vector[utf8(b"arm-recipe"), utf8(b"x86-recipe")],
            vector[utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")], vector[utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")], &clock, s.ctx());
        sui::clock::destroy_for_testing(clock); ts::return_shared(catalog); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut group = s.take_shared<DeviceGroup>(); let value = s.take_immutable<ComponentVersion>();
        let clock = sui::clock::create_for_testing(s.ctx());
        let mut record = customer::new_catalog_deployment(&c, &mut f, &value, vector[object::id(&group)], vector[], &clock, s.ctx());
        customer::begin_sharded_receipts(&c, &f, &mut record, s.ctx());
        let mut plan = customer::begin_revision(&c, &f, &group, s.ctx());
        desired_state::set_component(&mut plan, &mut record);
        customer::commit_revision(&c, &f, plan, &mut group, &clock, s.ctx());
        assert!(*desired_state::components(&group).get(&utf8(b"app")) == object::id(&record));
        assert!(deployment_manager::is_managed(&record));
        deployment_manager::share_deployment(record); sui::clock::destroy_for_testing(clock);
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(group); ts::return_immutable(value); s.end();
    }

    fun managed_extension_shard_cleanup(catalog: bool) {
        use sui_edge::deployment_manager as dm;
        use sui_edge::component_catalog::{Self as cc, ComponentCatalog, ComponentVersion};
        let mut s = setup(); let fid = fleet(&mut s); allow_devices(&mut s, 1);
        let did = register(&mut s, fid, @0xD1); s.next_tx(OWNER);
        cc::create_catalog(s.ctx()); s.next_tx(OWNER);
        let mut collection = s.take_shared<ComponentCatalog>();
        let clock = sui::clock::create_for_testing(s.ctx());
        cc::publish_version(&mut collection, utf8(b"app"), utf8(b"1.0.0"), vector[utf8(b"linux")],
            vector[utf8(b"x86_64")], vector[utf8(b"blob")],
            vector[utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")],
            vector[utf8(b"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")], &clock, s.ctx());
        sui::clock::destroy_for_testing(clock); ts::return_shared(collection); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let clock = sui::clock::create_for_testing(s.ctx());
        let mut d = if (catalog) {
            let version = s.take_immutable<ComponentVersion>();
            let d = customer::new_catalog_deployment(&c, &mut f, &version, vector[], vector[did], &clock, s.ctx());
            ts::return_immutable(version); d
        } else {
            let policy = dm::rollout_policy(1, 1, 1, 0, 1, 1000, 1000);
            customer::new_rollout(&c, &mut f, utf8(b"app"), utf8(b"1"), utf8(b"blob"), utf8(b"hash"),
                utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), vector[did], vector[0], policy, &clock, s.ctx())
        };
        sui::clock::destroy_for_testing(clock);
        customer::begin_sharded_receipts(&c, &f, &mut d, s.ctx());
        let roots = *dm::receipt_roots(&d);
        assert!(dm::receipt_schema(&d) == if (catalog) 2 else 3);
        dm::share_deployment(d); ts::return_shared(c); ts::return_shared(f); s.next_tx(OWNER);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid); let mut d = s.take_shared<dm::DeploymentRecord>();
        customer::begin_receipt_cleanup(&c, &f, &mut d, s.ctx());
        let mut n = 0;
        while (n < 16) { let root = s.take_shared_by_id<dm::ReceiptShard>(roots[n]); dm::retire_receipt_shard(&mut d, root); n = n + 1; };
        customer::delete_deployment(&c, &mut f, d, s.ctx());
        ts::return_shared(c); ts::return_shared(f); s.end();
    }
    #[test]
    fun managed_sharded_catalog_deletion_removes_relocated_binding() { managed_extension_shard_cleanup(true); }
    #[test]
    fun managed_sharded_rollout_deletion_removes_extensions() { managed_extension_shard_cleanup(false); }


    // SF-171 batch membership: bounded, all-or-none, fleet-bound and
    // optionally pinned to the reviewed architecture.
    fun batch_fixture(count: u64): (ts::Scenario, ID, vector<ID>) {
        let mut s = setup(); let a = fleet(&mut s);
        allow_devices(&mut s, count + 1);
        let mut ids = vector[];
        let mut i = 0;
        while (i < count) {
            let arch = if (i == 0) b"aarch64" else b"x86_64";
            ids.push_back(register_arch(&mut s, a, sui::address::from_u256(0xD00 + (i as u256)), arch));
            s.next_tx(OWNER); i = i + 1;
        };
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(a);
        let clock = sui::clock::create_for_testing(s.ctx());
        customer::create_group(&c, &mut f, utf8(b"Batch"), utf8(b""), &clock, s.ctx());
        sui::clock::destroy_for_testing(clock); ts::return_shared(c); ts::return_shared(f);
        s.next_tx(OWNER);
        (s, a, ids)
    }
    fun batch_add(s: &mut ts::Scenario, fid: ID, ids: vector<ID>, arch: Option<std::string::String>) {
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>(); let r = s.take_shared<DeviceRegistry>();
        customer::add_many_to_group(&c, &mut f, &mut g, &r, ids, arch, s.ctx());
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(g); ts::return_shared(r);
        s.next_tx(OWNER);
    }
    fun batch_remove(s: &mut ts::Scenario, fid: ID, ids: vector<ID>) {
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        customer::remove_many_from_group(&c, &mut f, &mut g, ids, s.ctx());
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(g);
        s.next_tx(OWNER);
    }
    fun members(s: &mut ts::Scenario): u64 {
        let g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        let n = sui_edge::device_group::member_count(&g);
        ts::return_shared(g); s.next_tx(OWNER); n
    }
    #[test]
    fun batch_add_and_remove_keep_group_accounting() {
        let (mut s, fid, ids) = batch_fixture(4);
        let x86 = vector[ids[1], ids[2], ids[3]];
        batch_add(&mut s, fid, x86, option::some(utf8(b"x86_64")));
        batch_add(&mut s, fid, vector[ids[0]], option::some(utf8(b"aarch64")));
        assert!(members(&mut s) == 4);
        batch_remove(&mut s, fid, ids);
        assert!(members(&mut s) == 0);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        customer::delete_group(&c, &mut f, g, s.ctx());
        ts::return_shared(c); ts::return_shared(f); s.end();
    }
    #[test]
    fun batch_add_without_arch_accepts_mixed_architectures() {
        let (mut s, fid, ids) = batch_fixture(3);
        batch_add(&mut s, fid, ids, option::none());
        assert!(members(&mut s) == 3);
        s.end();
    }
    #[test]
    fun batch_accepts_the_maximum_size() {
        let (mut s, fid, ids) = batch_fixture(customer::max_group_batch());
        batch_add(&mut s, fid, ids, option::none());
        assert!(members(&mut s) == customer::max_group_batch());
        batch_remove(&mut s, fid, ids);
        assert!(members(&mut s) == 0);
        s.end();
    }
    #[test, expected_failure(abort_code = 7, location = sui_edge::customer)]
    fun batch_rejects_more_than_the_maximum() {
        let (mut s, fid, ids) = batch_fixture(customer::max_group_batch() + 1);
        batch_add(&mut s, fid, ids, option::none());
        s.end();
    }
    #[test, expected_failure(abort_code = 7, location = sui_edge::customer)]
    fun batch_rejects_an_empty_add() {
        let (mut s, fid, _) = batch_fixture(1);
        batch_add(&mut s, fid, vector[], option::none());
        s.end();
    }
    #[test, expected_failure(abort_code = 7, location = sui_edge::customer)]
    fun batch_rejects_an_empty_remove() {
        let (mut s, fid, _) = batch_fixture(1);
        batch_remove(&mut s, fid, vector[]);
        s.end();
    }
    #[test, expected_failure(abort_code = 8, location = sui_edge::customer)]
    fun batch_rejects_an_architecture_mismatch() {
        let (mut s, fid, ids) = batch_fixture(3);
        // ids[0] is aarch64: the whole x86_64 batch aborts.
        batch_add(&mut s, fid, ids, option::some(utf8(b"x86_64")));
        s.end();
    }
    #[test, expected_failure(abort_code = 8, location = sui_edge::customer)]
    fun batch_arch_check_rejects_an_unregistered_id() {
        let (mut s, fid, _) = batch_fixture(1);
        batch_add(&mut s, fid, vector[object::id_from_address(@0xBAD)], option::some(utf8(b"x86_64")));
        s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::device_group)]
    fun batch_rejects_a_duplicate_device() {
        let (mut s, fid, ids) = batch_fixture(2);
        batch_add(&mut s, fid, vector[ids[1], ids[1]], option::none());
        s.end();
    }
    #[test, expected_failure(abort_code = 2, location = sui_edge::device_group)]
    fun batch_remove_rejects_a_non_member() {
        let (mut s, fid, ids) = batch_fixture(2);
        batch_add(&mut s, fid, vector[ids[1]], option::none());
        batch_remove(&mut s, fid, ids);
        s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun batch_rejects_a_foreign_fleet_device() {
        let (mut s, fid, ids) = batch_fixture(1);
        let other = fleet(&mut s);
        let foreign = register(&mut s, other, @0xF00); s.next_tx(OWNER);
        batch_add(&mut s, fid, vector[ids[0], foreign], option::none());
        s.end();
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::customer)]
    fun batch_rejects_a_non_administrator() {
        let (mut s, fid, ids) = batch_fixture(1);
        let mut c = s.take_shared<Customer>();
        customer::set_credential(&mut c, OTHER, true, false, s.ctx());
        ts::return_shared(c); s.next_tx(OTHER);
        batch_add(&mut s, fid, ids, option::none());
        s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun batch_rejects_a_foreign_registry() {
        let (mut s, fid, ids) = batch_fixture(1);
        device_registry::init_for_testing(s.ctx()); s.next_tx(OWNER);
        let foreign = ts::most_recent_id_shared<DeviceRegistry>().destroy_some();
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        let r = s.take_shared_by_id<DeviceRegistry>(foreign);
        customer::add_many_to_group(&c, &mut f, &mut g, &r, ids, option::none(), s.ctx());
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(g); ts::return_shared(r); s.end();
    }
    #[test]
    fun batch_changes_an_indexed_managed_group() {
        let (mut s, fid, da, db, _) = indexed_group();
        let c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        customer::adopt_indexed_group(&c, &mut f, &mut g, vector[da, db], s.ctx());
        customer::remove_many_from_group(&c, &mut f, &mut g, vector[da, db], s.ctx());
        assert!(sui_edge::device_group::member_count(&g) == 0);
        let r = s.take_shared<DeviceRegistry>();
        customer::add_many_to_group(&c, &mut f, &mut g, &r, vector[db, da], option::some(utf8(b"x86_64")), s.ctx());
        assert!(sui_edge::device_group::contains_device(&g, da) && sui_edge::device_group::member_count(&g) == 2);
        ts::return_shared(r); ts::return_shared(g); ts::return_shared(c); ts::return_shared(f); s.end();
    }
}
