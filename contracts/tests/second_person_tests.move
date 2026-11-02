#[test_only]
module sui_edge::second_person_tests {
    use std::bcs;
    use std::string::utf8;
    use sui::clock::{Self, Clock};
    use sui::test_scenario::{Self as ts, Scenario};
    use sui_edge::customer::{Self, Customer};
    use sui_edge::device_registry::{Self, DeviceRegistry};
    use sui_edge::security_policy::{Self as policy, Action};

    fun unenrolled(): Scenario {
        let mut s = ts::begin(@0xA);
        device_registry::init_for_testing(s.ctx());
        clock::create_for_testing(s.ctx()).share_for_testing(); s.next_tx(@0xA);
        let mut r = s.take_shared<DeviceRegistry>();
        customer::create(&mut r, utf8(b"two admins"), s.ctx()); ts::return_shared(r); s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>();
        customer::initialize_security(&mut c, s.ctx());
        customer::set_credential(&mut c, @0xB, true, true, s.ctx());
        customer::set_credential(&mut c, @0xC, true, false, s.ctx());
        ts::return_shared(c); s.next_tx(@0xA); s
    }
    fun setup(): Scenario {
        let mut s = unenrolled();
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let bytes = bcs::to_bytes(&vector[bcs::to_bytes(&utf8(b"configure_second_person")), bcs::to_bytes(&c)]);
        customer::propose_approval(&mut c, bytes, 1000, &clock, s.ctx());
        // The very first request seals authority, before any independent consent.
        let expected = bcs::to_bytes(&c);
        assert!(expected[32] == 199 && expected[33] == 0);
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xB);
        let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>(); let mut action = s.take_shared<Action>();
        customer::approve_action(&c, &mut action, &clock, s.ctx());
        ts::return_shared(action); ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>(); let action = s.take_shared<Action>();
        customer::configure_second_person(&mut c, action, &clock, s.ctx());
        customer::set_security(&mut c, vector[0,0,0,0,4,0,0], s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA); s
    }
    fun arguments(c: &Customer): vector<vector<u8>> {
        // Exact existing secured policy wrapper schema.
        vector[bcs::to_bytes(&utf8(b"set_security")), bcs::to_bytes(c), bcs::to_bytes(&vector[0u8,0,0,0,0,0,0])]
    }
    fun propose(s: &mut Scenario) {
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let args = bcs::to_bytes(&arguments(&c));
        customer::propose_approval(&mut c, args, 100, &clock, s.ctx());
        assert!(customer::pending_security_actions(&c).length() == 1);
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xB);
    }
    fun approve(s: &mut Scenario) {
        let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>(); let mut a = s.take_shared<Action>();
        customer::approve_action(&c, &mut a, &clock, s.ctx()); assert!(policy::attestations(&a) == 4);
        ts::return_shared(c); ts::return_shared(clock); ts::return_shared(a); s.next_tx(@0xA);
    }
    #[test]
    fun first_request_cancel_keeps_old_authority_sealed() {
        let mut s = unenrolled();
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let bytes = bcs::to_bytes(&vector[bcs::to_bytes(&utf8(b"configure_second_person")), bcs::to_bytes(&c)]);
        customer::propose_approval(&mut c, bytes, 1000, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>(); let a = s.take_shared<Action>();
        assert!(policy::disclosed_arguments(&a) == bcs::to_bytes(&vector[bcs::to_bytes(&utf8(b"configure_second_person")), bcs::to_bytes(&c)]));
        customer::cancel_disclosed_security_action(&mut c, a, s.ctx());
        assert!(customer::pending_security_actions(&c).is_empty());
        assert!(bcs::to_bytes(&c)[32] == 199);
        assert!(policy::requirements(&customer::security(&c)) == vector[0,0,0,0,0,0,0]);
        ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code = 9, location = sui_edge::customer)]
    fun first_request_rejects_wrong_customer_snapshot() {
        let mut s = unenrolled();
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let bytes = bcs::to_bytes(&vector[bcs::to_bytes(&utf8(b"configure_second_person")), vector[0u8]]);
        customer::propose_approval(&mut c, bytes, 1000, &clock, s.ctx()); abort 99
    }
    #[test]
    fun distinct_approval_executes_and_recovers_all_off() {
        let mut s = setup(); propose(&mut s); approve(&mut s);
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>(); let a = s.take_shared<Action>();
        customer::set_security_secured(&mut c, vector[0,0,0,0,0,0,0], a, &clock, s.ctx());
        assert!(customer::pending_security_actions(&c).is_empty());
        assert!(policy::requirements(&customer::security(&c)) == vector[0,0,0,0,0,0,0]);
        ts::return_shared(c); ts::return_shared(clock); s.end();
    }
    #[test, expected_failure(abort_code = 9, location = sui_edge::customer)]
    fun self_approval_refused() {
        let mut s = setup(); propose(&mut s); s.next_tx(@0xA);
        let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>(); let mut a = s.take_shared<Action>();
        customer::approve_action(&c, &mut a, &clock, s.ctx()); abort 99
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::customer)]
    fun member_approval_refused() {
        let mut s = setup(); propose(&mut s); s.next_tx(@0xC);
        let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>(); let mut a = s.take_shared<Action>();
        customer::approve_action(&c, &mut a, &clock, s.ctx()); abort 99
    }
    #[test, expected_failure(abort_code = 9, location = sui_edge::customer)]
    fun expired_approval_refused() {
        let mut s = setup(); propose(&mut s);
        let c = s.take_shared<Customer>(); let mut clock = s.take_shared<Clock>(); let mut a = s.take_shared<Action>();
        clock.set_for_testing(100); customer::approve_action(&c, &mut a, &clock, s.ctx()); abort 99
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::security_policy)]
    fun expired_execution_refused() {
        let mut s = setup(); propose(&mut s); approve(&mut s);
        let mut c = s.take_shared<Customer>(); let mut clock = s.take_shared<Clock>(); let a = s.take_shared<Action>();
        clock.set_for_testing(100); customer::set_security_secured(&mut c, vector[0,0,0,0,0,0,0], a, &clock, s.ctx()); abort 99
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun unapproved_execution_refused() {
        let mut s = setup(); propose(&mut s); s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>(); let a = s.take_shared<Action>();
        customer::set_security_secured(&mut c, vector[0,0,0,0,0,0,0], a, &clock, s.ctx()); abort 99
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::security_policy)]
    fun substituted_policy_refused() {
        let mut s = setup(); propose(&mut s); approve(&mut s);
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>(); let a = s.take_shared<Action>();
        customer::set_security_secured(&mut c, vector[4,0,0,0,0,0,0], a, &clock, s.ctx()); abort 99
    }
    #[test]
    fun rejection_removes_pending() {
        let mut s = setup(); propose(&mut s);
        let mut c = s.take_shared<Customer>(); let a = s.take_shared<Action>();
        customer::reject_action(&mut c, a, s.ctx()); assert!(customer::pending_security_actions(&c).is_empty());
        ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun direct_weakening_refused() {
        let mut s = setup(); let mut c = s.take_shared<Customer>();
        customer::set_security(&mut c, vector[0,0,0,0,0,0,0], s.ctx()); abort 99
    }
    #[test, expected_failure(abort_code = 9, location = sui_edge::customer)]
    fun stale_generation_cannot_approve() {
        let mut s = setup(); propose(&mut s);
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let a = s.take_shared<Action>(); let stale = object::id(&a); ts::return_shared(a);
        // A second pending action independently authorizes a generation change.
        let args = bcs::to_bytes(&arguments(&c));
        customer::propose_approval(&mut c, args, 100, &clock, s.ctx());
        let ids = customer::pending_security_actions(&c); let fresh = ids[1];
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA);
        let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let mut a = s.take_shared_by_id<Action>(fresh);
        customer::approve_action(&c, &mut a, &clock, s.ctx());
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xB);
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>(); let a = s.take_shared_by_id<Action>(fresh);
        customer::set_security_secured(&mut c, vector[0,0,0,0,0,0,0], a, &clock, s.ctx());
        let mut a = s.take_shared_by_id<Action>(stale);
        customer::approve_action(&c, &mut a, &clock, s.ctx()); abort 99
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::security_policy)]
    fun immutable_legacy_cancel_cannot_orphan_pending_index() {
        let mut s = setup(); propose(&mut s);
        let c = s.take_shared<Customer>(); let a = s.take_shared<Action>();
        customer::cancel_security_action(&c, a, s.ctx()); abort 99
    }

}
