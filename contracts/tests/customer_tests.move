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
    fun register(s: &mut ts::Scenario, fid: ID, address: address): ID {
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared_by_id<Fleet>(fid);
        let mut r = s.take_shared<DeviceRegistry>();
        let mut b = s.take_shared_by_id<FleetBudget>(customer::fleet_budget(&f));
        let clock = sui::clock::create_for_testing(s.ctx());
        customer::register_device(&mut c, &mut f, &mut b, &mut r, utf8(b"Device"), address, utf8(b"x86_64"), &clock, s.ctx());
        let ids = customer::devices(&f);
        let did = ids[ids.length() - 1];
        sui::clock::destroy_for_testing(clock);
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(b); ts::return_shared(r);
        did
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

}
