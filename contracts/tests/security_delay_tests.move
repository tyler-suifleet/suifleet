#[test_only]
module sui_edge::security_delay_tests {
    use std::bcs;
    use sui::clock;
    use sui::test_scenario as ts;
    use std::string::utf8;
    use sui_edge::customer::{Self, Customer};
    use sui_edge::device_registry::{Self, DeviceRegistry};
    use sui_edge::security_policy::{Self as policy, Action};

    fun arguments(): vector<vector<u8>> { vector[bcs::to_bytes(&100u64)] }
    fun setup(): (ts::Scenario, UID) {
        let mut s = ts::begin(@0xA);
        let mut uid = object::new(s.ctx());
        policy::initialize(&mut uid);
        policy::configure_delays(&mut uid, vector[1000,2000,3000,4000,5000,6000,7000], s.ctx());
        policy::update(&mut uid, vector[8,8,8,8,8,8,8], s.ctx());
        let clock = clock::create_for_testing(s.ctx());
        policy::prepare(&uid, policy::action_intent(uid.to_inner(), bcs::to_bytes(&arguments())), 10000, &clock, s.ctx());
        clock.destroy_for_testing();
        s.next_tx(@0xA);
        (s, uid)
    }
    fun execute(elapsed: u64, class: u64, secondary: bool) {
        let (mut s, mut uid) = setup();
        let a = s.take_shared<Action>();
        assert!(policy::queued_at(&a) == 0);
        let mut clock = clock::create_for_testing(s.ctx()); clock.increment_for_testing(elapsed);
        policy::begin_scoped(&mut uid, a, arguments(), class, false, &clock, s.ctx());
        policy::check(&uid, if (class == 7) 6 else class, s.ctx());
        if (secondary) policy::check(&uid, 6, s.ctx());
        policy::finish(&mut uid);
        clock.destroy_for_testing(); policy::remove_for_testing(&mut uid); uid.delete(); s.end();
    }
    #[test] fun exact_boundary_allowed() { execute(1000, 0, false); }
    #[test] fun shorter_class_does_not_wait_for_unrelated_classes() { execute(1000, 0, false); }
    #[test] fun union_waits_for_longest_class() { execute(7000, 7, false); }
    #[test, expected_failure(abort_code = 3, location = sui_edge::security_policy)]
    fun early_refused() { execute(999, 0, false); }
    #[test, expected_failure(abort_code = 3, location = sui_edge::security_policy)]
    fun union_early_refused() { execute(6999, 7, false); }
    #[test, expected_failure(abort_code = 3, location = sui_edge::security_policy)]
    fun secondary_class_cannot_inherit_shorter_delay() { execute(1000, 0, true); }
    #[test, expected_failure(abort_code = 0, location = sui_edge::security_policy)]
    fun expired_action_refused() { execute(10000, 0, false); }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun direct_policy_weakening_refused() {
        let (mut s, mut uid) = setup(); policy::update(&mut uid, vector[0,0,0,0,0,0,0], s.ctx());
        policy::remove_for_testing(&mut uid); uid.delete(); s.end();
    }
    #[test, expected_failure(abort_code = 3, location = sui_edge::security_policy)]
    fun configuration_below_bound_refused() {
        let mut s = ts::begin(@0xA); let mut uid = object::new(s.ctx()); policy::initialize(&mut uid);
        policy::configure_delays(&mut uid, vector[999,1000,1000,1000,1000,1000,1000], s.ctx());
        policy::remove_for_testing(&mut uid); uid.delete(); s.end();
    }
    #[test, expected_failure(abort_code = 3, location = sui_edge::security_policy)]
    fun configuration_above_bound_refused() {
        let mut s = ts::begin(@0xA); let mut uid = object::new(s.ctx()); policy::initialize(&mut uid);
        policy::configure_delays(&mut uid, vector[518400001,1000,1000,1000,1000,1000,1000], s.ctx());
        policy::remove_for_testing(&mut uid); uid.delete(); s.end();
    }
    #[test] fun mature_recovery_requires_current_delay() {
        let (mut s, mut uid) = setup(); let a = s.take_shared<Action>();
        let mut clock = clock::create_for_testing(s.ctx()); clock.increment_for_testing(7000);
        policy::begin(&mut uid, a, arguments(), &clock, s.ctx());
        policy::update(&mut uid, vector[0,0,0,0,0,0,0], s.ctx());
        policy::finish(&mut uid);
        assert!(policy::requirements(&policy::read(&uid)) == vector[0,0,0,0,0,0,0]);
        clock.destroy_for_testing(); policy::remove_for_testing(&mut uid); uid.delete(); s.end();
    }
    fun customer_setup(): ts::Scenario {
        let mut s = ts::begin(@0xA);
        device_registry::init_for_testing(s.ctx()); s.next_tx(@0xA);
        let mut registry = s.take_shared<DeviceRegistry>();
        customer::create(&mut registry, utf8(b"delay"), s.ctx()); ts::return_shared(registry);
        s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>();
        customer::set_credential(&mut c, @0xB, true, true, s.ctx());
        customer::configure_security_delays(&mut c, vector[1000,1000,1000,1000,1000,1000,1000], s.ctx());
        customer::set_security(&mut c, vector[8,8,8,8,8,8,8], s.ctx());
        let clock = clock::create_for_testing(s.ctx());
        customer::prepare_security_action(&c, policy::action_intent(object::id(&c), bcs::to_bytes(&arguments())), 10000, &clock, s.ctx());
        clock.destroy_for_testing(); ts::return_shared(c); s.next_tx(@0xB); s
    }
    #[test] fun different_current_admin_cancels_without_waiting() {
        let mut s = customer_setup(); let c = s.take_shared<Customer>(); let a = s.take_shared<Action>();
        customer::cancel_security_action(&c, a, s.ctx()); ts::return_shared(c);
        s.next_tx(@0xB); assert!(!ts::has_most_recent_shared<Action>()); s.end();
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::customer)]
    fun outsider_cannot_cancel() {
        let mut s = customer_setup(); s.next_tx(@0xC);
        let c = s.take_shared<Customer>(); let a = s.take_shared<Action>();
        customer::cancel_security_action(&c, a, s.ctx()); ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun delay_reconfiguration_uses_current_union() {
        let (mut s, mut uid) = setup();
        policy::configure_delays(&mut uid, vector[1000,1000,1000,1000,1000,1000,1000], s.ctx());
        policy::remove_for_testing(&mut uid); uid.delete(); s.end();
    }

}
