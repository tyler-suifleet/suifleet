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
        customer::set_security(&mut c, vector[8,0,0,0,0,0,0], s.ctx());
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
        policy::prepare(&uid, policy::action_intent(uid.to_inner(), bcs::to_bytes(&intent())), 100, &clock, s.ctx());
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

    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun capability_loan_cannot_inherit_execution_grant() {
        let mut s = ts::begin(@0xA); let mut uid = kernel(&mut s, 1);
        let mut a = s.take_shared<Action>(); policy::attest(&mut a, 1);
        let clock = clock::create_for_testing(s.ctx());
        policy::begin(&mut uid, a, intent(), &clock, s.ctx());
        policy::assert_unprotected(&uid);
        clock.destroy_for_testing(); clean(uid); s.end();
    }
    #[test]
    fun intent_domain_is_customer_specific() {
        assert!(policy::action_intent(@0xA.to_id(), bcs::to_bytes(&intent())) != policy::action_intent(@0xB.to_id(), bcs::to_bytes(&intent())));
    }
    #[test]
    fun unrelated_class_does_not_require_its_factors() {
        let mut s = ts::begin(@0xA); let mut uid = kernel(&mut s, 0);
        policy::set_for_testing(&mut uid, vector[1,0,0,0,0,0,0]);
        let a = s.take_shared<Action>(); let clock = clock::create_for_testing(s.ctx());
        policy::begin_scoped(&mut uid, a, intent(), 3, false, &clock, s.ctx());
        policy::check(&uid, 3, s.ctx());
        policy::finish(&mut uid);
        clock.destroy_for_testing(); clean(uid); s.end();
    }
    #[test]
    fun pause_skips_delay_but_keeps_other_factors() {
        let mut s = ts::begin(@0xA); let mut uid = kernel(&mut s, 0);
        policy::set_for_testing(&mut uid, vector[0,9,0,0,0,0,0]);
        let mut a = s.take_shared<Action>(); policy::attest(&mut a, 1);
        let clock = clock::create_for_testing(s.ctx());
        policy::begin_scoped(&mut uid, a, intent(), 1, true, &clock, s.ctx());
        policy::check(&uid, 1, s.ctx());
        policy::finish(&mut uid);
        clock.destroy_for_testing(); clean(uid); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun pause_cannot_skip_passkey() {
        let mut s = ts::begin(@0xA); let mut uid = kernel(&mut s, 0);
        policy::set_for_testing(&mut uid, vector[0,10,0,0,0,0,0]);
        let a = s.take_shared<Action>(); let clock = clock::create_for_testing(s.ctx());
        policy::begin_scoped(&mut uid, a, intent(), 1, true, &clock, s.ctx());
        clock.destroy_for_testing(); clean(uid); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun resume_requires_delay() {
        let mut s = ts::begin(@0xA); let mut uid = kernel(&mut s, 0);
        policy::set_for_testing(&mut uid, vector[0,8,0,0,0,0,0]);
        let a = s.take_shared<Action>(); let clock = clock::create_for_testing(s.ctx());
        policy::begin_scoped(&mut uid, a, intent(), 1, false, &clock, s.ctx());
        clock.destroy_for_testing(); clean(uid); s.end();
    }
    #[test]
    fun recipient_confirmed_handover_retains_namespace() {
        let mut s = setup(); s.next_tx(@0xB);
        let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let fleets = *customer::fleets(&c);
        customer::confirm_handover(&c, 100, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let consent = s.take_shared<customer::HandoverConsent>();
        customer::handover(&mut c, consent, &clock, s.ctx());
        assert!(*customer::fleets(&c) == fleets);
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xB);
        let c = s.take_shared<Customer>(); customer::assert_admin(&c, s.ctx());
        ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::customer)]
    fun source_loses_authority_after_handover() {
        let mut s = setup(); s.next_tx(@0xB);
        let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        customer::confirm_handover(&c, 100, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let consent = s.take_shared<customer::HandoverConsent>();
        customer::handover(&mut c, consent, &clock, s.ctx());
        customer::assert_admin(&c, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun stale_recipient_consent_is_rejected() {
        let mut s = setup(); s.next_tx(@0xB);
        let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        customer::confirm_handover(&c, 100, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let consent = s.take_shared<customer::HandoverConsent>();
        customer::set_credential(&mut c, @0xC, true, false, s.ctx());
        customer::handover(&mut c, consent, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.end();
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::component_catalog)]
    fun old_catalog_publisher_is_excluded_after_adoption() {
        let mut s = setup();
        sui_edge::component_catalog::create_catalog(s.ctx()); s.next_tx(@0xA);
        let c = s.take_shared<Customer>(); let mut catalog = s.take_shared<sui_edge::component_catalog::ComponentCatalog>();
        customer::adopt_catalog(&c, &mut catalog, s.ctx());
        let clock = s.take_shared<Clock>();
        sui_edge::component_catalog::publish_version(&mut catalog, utf8(b"a"), utf8(b"1.0.0"),
            vector[], vector[], vector[], vector[], vector[], &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(catalog); ts::return_shared(clock); s.end();
    }

    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun administrator_addition_requires_stronger_other_classes() {
        let mut s = setup(); let mut c = s.take_shared<Customer>();
        customer::set_security_requirements_for_testing(&mut c, vector[1,0,0,0,0,0,0]);
        customer::set_credential(&mut c, @0xB, true, true, s.ctx());
        ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun raw_deployment_cannot_bypass_publication_policy() {
        let mut s = setup(); let mut c = s.take_shared<Customer>();
        customer::set_security_requirements_for_testing(&mut c, vector[1,0,0,0,0,0,0]);
        let mut fleet = s.take_shared<customer::Fleet>(); let clock = s.take_shared<Clock>();
        let d = customer::new_deployment(&c, &mut fleet, utf8(b"a"), utf8(b"1.0.0"), utf8(b"blob"),
            utf8(b"ab"), utf8(b"cd"), vector[], vector[], &clock, s.ctx());
        sui_edge::deployment_manager::share_deployment(d);
        ts::return_shared(c); ts::return_shared(fleet); ts::return_shared(clock); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun factor_source_replacement_uses_current_policy() {
        let mut s = ts::begin(@0xA); let mut uid = kernel(&mut s, 1);
        policy::configure_factor(&mut uid, 1, vector[1,2,3], s.ctx());
        clean(uid); s.end();
    }

    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun group_adoption_cannot_bypass_deployment_policy() {
        let mut s = setup();
        let clock = s.take_shared<Clock>();
        sui_edge::device_group::create_group(utf8(b"external"), utf8(b""), &clock, s.ctx());
        ts::return_shared(clock);
        s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared<customer::Fleet>();
        let mut g = s.take_shared<sui_edge::device_group::DeviceGroup>();
        customer::set_security_requirements_for_testing(&mut c, vector[0,1,0,0,0,0,0]);
        customer::adopt_group(&c, &mut f, &mut g, s.ctx());
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(g); s.end();
    }

    public struct ForgedStorageSource has drop {}
    #[test, expected_failure(abort_code = 0, location = sui_edge::customer)]
    fun arbitrary_source_cannot_open_storage_grant() {
        let mut s = setup(); let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        customer::prepare_security_action(&c, policy::action_intent(object::id(&c), bcs::to_bytes(&intent())), 100, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>(); let mut f = s.take_shared<customer::Fleet>(); let clock = s.take_shared<Clock>();
        let action = s.take_shared<Action>();
        let (cap, loan) = customer::begin_storage_action(&mut c, &mut f, action, bcs::to_bytes(&intent()), &clock, ForgedStorageSource {}, s.ctx());
        customer::finish_storage_action(&mut c, &mut f, cap, loan);
        ts::return_shared(c); ts::return_shared(f); ts::return_shared(clock); s.end();
    }
}
