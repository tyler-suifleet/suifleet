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
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun enrolled_administrator_cannot_use_another_administrators_passkey() {
        let mut s = setup();
        let mut c = s.take_shared<Customer>();
        customer::set_credential(&mut c, @0xB, true, true, s.ctx());
        ts::return_shared(c); s.next_tx(@0xA);
        enable(&mut s);
        s.next_tx(@0xB); sponsor(&mut s, factor::key_address(public_key()));
        let c = s.take_shared<Customer>(); let mut f = s.take_shared<Fleet>();
        customer::rename_fleet(&c, &mut f, utf8(b"Wrong administrator binding"), s.ctx());
        ts::return_shared(c); ts::return_shared(f); s.end();
    }

    fun sealed_authority(): Scenario {
        let mut s = setup();
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let arguments = std::bcs::to_bytes(&vector[
            std::bcs::to_bytes(&utf8(b"configure_second_person")), std::bcs::to_bytes(&object::id(&c))]);
        customer::propose_approval(&mut c, arguments, 1000, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>(); let action = s.take_shared<security_policy::Action>();
        customer::cancel_disclosed_security_action(&mut c, action, s.ctx());
        assert!(std::bcs::to_bytes(&c)[32] == 199);
        ts::return_shared(c); s.next_tx(@0xA); s
    }
    #[test]
    fun passkey_enrollment_and_recovery_after_second_person_seals_authority() {
        let mut s = sealed_authority(); enable(&mut s);
        sponsor(&mut s, factor::key_address(public_key()));
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared<Fleet>();
        customer::rename_fleet(&c, &mut f, utf8(b"Sealed passkey action"), s.ctx());
        customer::set_security(&mut c, vector[0,0,0,0,0,0,0], s.ctx());
        factor::remove(&mut c, @0xA, factor::key_address(public_key()), s.ctx());
        assert!(factor::keys(&c).is_empty());
        ts::return_shared(c); ts::return_shared(f); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun sealed_authority_still_requires_native_passkey() {
        let mut s = sealed_authority(); enable(&mut s);
        let c = s.take_shared<Customer>(); let mut f = s.take_shared<Fleet>();
        customer::rename_fleet(&c, &mut f, utf8(b"Wallet alone"), s.ctx());
        ts::return_shared(c); ts::return_shared(f); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun native_passkey_cannot_replace_required_second_person_approval() {
        let mut s = sealed_authority(); enable(&mut s);
        let mut c = s.take_shared<Customer>();
        customer::set_security_requirements_for_testing(&mut c, vector[6,6,6,6,6,6,6]);
        ts::return_shared(c); s.next_tx(@0xA);
        sponsor(&mut s, factor::key_address(public_key()));
        let c = s.take_shared<Customer>(); let mut f = s.take_shared<Fleet>();
        customer::rename_fleet(&c, &mut f, utf8(b"Missing independent approval"), s.ctx());
        ts::return_shared(c); ts::return_shared(f); s.end();
    }

    fun combined_action(): Scenario {
        let mut s = sealed_authority(); enroll(&mut s);
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        customer::set_security_requirements_for_testing(&mut c, vector[6,6,6,6,6,6,6]);
        let arguments = vector[std::bcs::to_bytes(&utf8(b"set_security")),
            std::bcs::to_bytes(&object::id(&c)), std::bcs::to_bytes(&vector[0u8,0,0,0,0,0,0])];
        let intent = security_policy::action_intent(object::id(&c), std::bcs::to_bytes(&arguments));
        customer::prepare_security_action(&c, intent, 1000, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA);
        // Kernel regression: package attestation models the independent verifier.
        // Its distinct-administrator checks are covered by second_person_tests.
        let mut action = s.take_shared<security_policy::Action>();
        security_policy::attest(&mut action, 4);
        ts::return_shared(action); s.next_tx(@0xA); s
    }
    #[test]
    fun native_passkey_and_independent_attestation_combine() {
        let mut s = combined_action(); sponsor(&mut s, factor::key_address(public_key()));
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let action = s.take_shared<security_policy::Action>();
        customer::set_security_secured(&mut c, vector[0,0,0,0,0,0,0], action, &clock, s.ctx());
        assert!(security_policy::requirements(&customer::security(&c)) == vector[0,0,0,0,0,0,0]);
        ts::return_shared(c); ts::return_shared(clock); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun independent_attestation_cannot_replace_native_passkey() {
        let mut s = combined_action();
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let action = s.take_shared<security_policy::Action>();
        customer::set_security_secured(&mut c, vector[0,0,0,0,0,0,0], action, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.end();
    }

    fun delayed_passkey_action(): Scenario {
        let mut s = setup(); enroll(&mut s);
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        customer::configure_security_delays(&mut c, vector[1000,1000,1000,1000,1000,1000,1000], s.ctx());
        customer::set_security_requirements_for_testing(&mut c, vector[10,10,10,10,10,10,10]);
        let arguments = vector[std::bcs::to_bytes(&utf8(b"set_security")),
            std::bcs::to_bytes(&object::id(&c)), std::bcs::to_bytes(&vector[0u8,0,0,0,0,0,0])];
        let intent = security_policy::action_intent(object::id(&c), std::bcs::to_bytes(&arguments));
        customer::prepare_security_action(&c, intent, 10000, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA); s
    }
    fun execute_delayed_passkey(s: &mut Scenario, elapsed: u64) {
        let mut c = s.take_shared<Customer>(); let mut clock = s.take_shared<Clock>();
        clock.increment_for_testing(elapsed);
        let action = s.take_shared<security_policy::Action>();
        customer::set_security_secured(&mut c, vector[0,0,0,0,0,0,0], action, &clock, s.ctx());
        assert!(security_policy::requirements(&customer::security(&c)) == vector[0,0,0,0,0,0,0]);
        ts::return_shared(c); ts::return_shared(clock);
    }
    #[test]
    fun native_passkey_and_elapsed_delay_combine() {
        let mut s = delayed_passkey_action(); sponsor(&mut s, factor::key_address(public_key()));
        execute_delayed_passkey(&mut s, 1000); s.end();
    }
    #[test, expected_failure(abort_code = 3, location = sui_edge::security_policy)]
    fun native_passkey_cannot_skip_delay() {
        let mut s = delayed_passkey_action(); sponsor(&mut s, factor::key_address(public_key()));
        execute_delayed_passkey(&mut s, 999); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun elapsed_delay_cannot_replace_native_passkey() {
        let mut s = delayed_passkey_action();
        execute_delayed_passkey(&mut s, 1000); s.end();
    }

}
