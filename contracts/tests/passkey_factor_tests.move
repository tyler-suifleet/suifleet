#[test_only]
module sui_edge::passkey_factor_tests {
    use std::string::utf8;
    use sui::clock::{Self, Clock};
    use sui::test_scenario::{Self as ts, Scenario};
    use sui_edge::customer::{Self, Customer, Fleet};
    use sui_edge::device_registry::{Self, DeviceRegistry};
    use sui_edge::passkey_factor::{Self as factor, Enrollment};
    use sui_edge::security_policy;

    fun public_key(): vector<u8> { x"036b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296" }
    fun setup(): Scenario {
        let mut s = ts::begin(@0xA);
        device_registry::init_for_testing(s.ctx());
        clock::create_for_testing(s.ctx()).share_for_testing();
        s.next_tx(@0xA);
        let mut r = s.take_shared<DeviceRegistry>();
        customer::create(&mut r, utf8(b"Passkey organization"), s.ctx());
        ts::return_shared(r); s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>();
        customer::initialize_security(&mut c, s.ctx());
        assert!(security_policy::requirements(&customer::security(&c)) == vector[0,0,0,0,0,0,0]);
        ts::return_shared(c); s.next_tx(@0xA); s
    }
    fun prove(s: &mut Scenario, owner: address) {
        s.next_tx(factor::key_address(public_key()));
        let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        factor::prove_enrollment(&c, owner, public_key(), 1000, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA);
    }
    fun enroll(s: &mut Scenario) {
        prove(s, @0xA);
        let mut c = s.take_shared<Customer>(); let proof = s.take_shared<Enrollment>(); let clock = s.take_shared<Clock>();
        factor::enroll(&mut c, proof, &clock, s.ctx());
        assert!(factor::keys(&c) == vector[@0xA, factor::key_address(public_key())]);
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA);
    }
    fun enable(s: &mut Scenario) {
        enroll(s);
        let mut c = s.take_shared<Customer>();
        customer::set_security(&mut c, vector[2,2,2,2,2,2,2], s.ctx());
        ts::return_shared(c); s.next_tx(@0xA);
    }
    fun sponsor(s: &mut Scenario, key: address) {
        let builder = s.ctx_builder().set_sponsor(key);
        s.next_with_context(builder);
    }
    #[test]
    fun native_cosign_allows_action_and_recovery_disable_then_remove() {
        let mut s = setup(); enable(&mut s); sponsor(&mut s, factor::key_address(public_key()));
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared<Fleet>();
        customer::rename_fleet(&c, &mut f, utf8(b"Authenticated change"), s.ctx());
        customer::set_security(&mut c, vector[0,0,0,0,0,0,0], s.ctx());
        factor::remove(&mut c, @0xA, factor::key_address(public_key()), s.ctx());
        assert!(factor::keys(&c).is_empty());
        // Empty enrollment is unavailable, not an active recovery source.
        ts::return_shared(c); ts::return_shared(f); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun wallet_alone_refused() {
        let mut s = setup(); enable(&mut s);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared<Fleet>();
        customer::rename_fleet(&c, &mut f, utf8(b"Attack"), s.ctx());
        ts::return_shared(c); ts::return_shared(f); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun wrong_native_sponsor_refused() {
        let mut s = setup(); enable(&mut s); sponsor(&mut s, @0xB);
        let c = s.take_shared<Customer>(); customer::assert_admin(&c, s.ctx());
        ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::passkey_factor)]
    fun enrollment_requires_native_passkey_sender() {
        let mut s = setup();
        let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        factor::prove_enrollment(&c, @0xA, public_key(), 1000, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.end();
    }
    #[test, expected_failure(abort_code = 2, location = sui_edge::passkey_factor)]
    fun last_key_cannot_be_removed_while_required() {
        let mut s = setup(); enable(&mut s); sponsor(&mut s, factor::key_address(public_key()));
        let mut c = s.take_shared<Customer>();
        factor::remove(&mut c, @0xA, factor::key_address(public_key()), s.ctx());
        ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun rotation_cannot_be_reset_with_wallet_alone() {
        let mut s = setup(); enable(&mut s);
        let mut c = s.take_shared<Customer>();
        factor::remove(&mut c, @0xA, factor::key_address(public_key()), s.ctx());
        ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun empty_binding_cannot_activate() {
        let mut s = setup(); enroll(&mut s);
        let mut c = s.take_shared<Customer>();
        factor::remove(&mut c, @0xA, factor::key_address(public_key()), s.ctx());
        customer::set_security(&mut c, vector[2,0,0,0,0,0,0], s.ctx());
        ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::passkey_factor)]
    fun stale_generation_cannot_enroll() {
        let mut s = setup(); prove(&mut s, @0xA);
        let mut c = s.take_shared<Customer>(); let proof = s.take_shared<Enrollment>(); let clock = s.take_shared<Clock>();
        customer::set_credential(&mut c, @0xB, true, true, s.ctx());
        factor::enroll(&mut c, proof, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.end();
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::customer)]
    fun other_administrator_passkey_does_not_replace_wallet_authority() {
        let mut s = setup(); enable(&mut s); s.next_tx(@0xB); sponsor(&mut s, factor::key_address(public_key()));
        let c = s.take_shared<Customer>(); customer::assert_admin(&c, s.ctx());
        ts::return_shared(c); s.end();
    }
}
