#[test_only]
module sui_edge::security_policy_tests {
    use std::bcs;
    use std::string::utf8;
    use sui::clock::{Self, Clock};
    use sui::hash;
    use sui::test_scenario::{Self as ts};
    use sui_edge::security_policy::{Self as policy, Action};
    use sui_edge::customer::{Self, Customer};
    use sui_edge::device_registry::{Self, DeviceRegistry};

    fun setup(): ts::Scenario {
        let mut s = ts::begin(@0xA);
        device_registry::init_for_testing(s.ctx());
        clock::create_for_testing(s.ctx()).share_for_testing();
        s.next_tx(@0xA);
        let mut r = s.take_shared<DeviceRegistry>();
        customer::create(&mut r, utf8(b"protected"), s.ctx());
        ts::return_shared(r);
        s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>();
        customer::initialize_security(&mut c, s.ctx());
        assert!(policy::requirements(&customer::security(&c)) == vector[0,0,0,0,0,0,0]);
        customer::assert_admin(&c, s.ctx());
        ts::return_shared(c);
        s.next_tx(@0xA);
        s
    }
    #[test]
    fun migration_defaults_and_current_credentials() {
        let mut s = setup();
        let mut c = s.take_shared<Customer>();
        customer::set_credential(&mut c, @0xB, true, true, s.ctx());
        assert!(policy::generation(&customer::security(&c)) == 1);
        ts::return_shared(c); s.next_tx(@0xB);
        let c = s.take_shared<Customer>();
        customer::assert_admin(&c, s.ctx());
        ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code = 2, location = sui_edge::security_policy)]
    fun missing_child_verifier_cannot_be_enabled() {
        let mut s = setup(); let mut c = s.take_shared<Customer>();
        customer::set_security(&mut c, vector[1,0,0,0,0,0,0], s.ctx());
        ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::customer)]
    fun nonadministrator_cannot_prepare() {
        let mut s = setup(); s.next_tx(@0xB);
        let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        customer::prepare_security_action(&c, vector[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0], 10, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.end();
    }
    fun intent(): vector<vector<u8>> { vector[bcs::to_bytes(&utf8(b"withdraw")), bcs::to_bytes(&100u64)] }
    fun kernel(s: &mut ts::Scenario, mask: u8): UID {
        let mut uid = object::new(s.ctx());
        policy::initialize(&mut uid);
        policy::set_for_testing(&mut uid, vector[mask,mask,mask,mask,mask,mask,mask]);
        let clock = clock::create_for_testing(s.ctx());
        policy::prepare(&uid, hash::blake2b256(&bcs::to_bytes(&intent())), 100, &clock, s.ctx());
        clock.destroy_for_testing();
        s.next_tx(@0xA);
        uid
    }
    fun clean(uid: UID) {
        let mut uid = uid;
        policy::remove_for_testing(&mut uid);
        uid.delete();
    }
    #[test]
    fun all_sixteen_combinations_and_cleared_grants() {
        let mut mask = 0;
        while (mask < 16) {
            let mut s = ts::begin(@0xA);
            let mut uid = kernel(&mut s, mask);
            let mut a = s.take_shared<Action>();
            let mut factor = 1;
            while (factor <= 8) { if ((mask & factor) != 0) policy::attest(&mut a, factor); factor = factor * 2; };
            let clock = clock::create_for_testing(s.ctx());
            policy::begin(&mut uid, a, intent(), &clock, s.ctx());
            policy::check_all(&uid, s.ctx());
            policy::finish(&mut uid);
            assert!(policy::generation(&policy::read(&uid)) == 1);
            clock.destroy_for_testing(); clean(uid); s.end();
            mask = mask + 1;
        };
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun missing_factor_cannot_execute() {
        let mut s = ts::begin(@0xA); let mut uid = kernel(&mut s, 15);
        let mut a = s.take_shared<Action>();
        policy::attest(&mut a, 1); policy::attest(&mut a, 2); policy::attest(&mut a, 4);
        let clock = clock::create_for_testing(s.ctx());
        policy::begin(&mut uid, a, intent(), &clock, s.ctx());
        clock.destroy_for_testing(); clean(uid); s.end();
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::security_policy)]
    fun changed_amount_cannot_execute() {
        let mut s = ts::begin(@0xA); let mut uid = kernel(&mut s, 0);
        let a = s.take_shared<Action>(); let clock = clock::create_for_testing(s.ctx());
        policy::begin(&mut uid, a, vector[bcs::to_bytes(&utf8(b"withdraw")), bcs::to_bytes(&101u64)], &clock, s.ctx());
        clock.destroy_for_testing(); clean(uid); s.end();
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::security_policy)]
    fun changed_generation_cannot_execute() {
        let mut s = ts::begin(@0xA); let mut uid = kernel(&mut s, 0);
        policy::invalidate(&mut uid);
        let a = s.take_shared<Action>(); let clock = clock::create_for_testing(s.ctx());
        policy::begin(&mut uid, a, intent(), &clock, s.ctx());
        clock.destroy_for_testing(); clean(uid); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun grant_does_not_escape_wrapper() {
        let mut s = ts::begin(@0xA); let mut uid = kernel(&mut s, 1);
        let mut a = s.take_shared<Action>(); policy::attest(&mut a, 1);
        let clock = clock::create_for_testing(s.ctx());
        policy::begin(&mut uid, a, intent(), &clock, s.ctx());
        policy::finish(&mut uid);
        policy::check_all(&uid, s.ctx());
        clock.destroy_for_testing(); clean(uid); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun weakening_uses_current_requirements() {
        let mut s = ts::begin(@0xA); let mut uid = kernel(&mut s, 1);
        policy::update(&mut uid, vector[0,0,0,0,0,0,0], s.ctx());
        clean(uid); s.end();
    }
    #[test]
    fun current_administrator_can_cancel_without_factors() {
        let mut s = ts::begin(@0xA); let uid = kernel(&mut s, 15);
        let a = s.take_shared<Action>(); policy::cancel(&uid, a);
        clean(uid); s.end();
    }
}
