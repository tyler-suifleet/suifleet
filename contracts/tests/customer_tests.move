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
}
