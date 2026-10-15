#[test_only]
module sui_edge::enrollment_roles_tests {
    use std::string;
    use sui::clock;
    use sui::test_scenario::{Self as ts};
    use sui_edge::deployment_manager::{Self, DeploymentRecord};
    use sui_edge::device_cap::{Self, DeviceCap};
    use sui_edge::device_group::{Self, DeviceGroup};
    use sui_edge::device_registry::{Self, DeviceRegistry, DeviceOwnerCap};
    use sui_edge::enrollment::{Self, EnrollmentPolicy};
    use sui_edge::roles::{Self, DeployerRole};

    const OPERATOR: address = @0xA1;
    const DEVICE: address = @0xD1;
    const DEVICE_2: address = @0xD2;
    const CLAIM: address = @0xC1;
    const CLAIM_2: address = @0xC2;
    const STRANGER: address = @0xBAD;
    const DELEGATE: address = @0xDE;
    const DIGEST: vector<u8> = b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fun setup(quota: u64, expires: u64): ts::Scenario {
        let mut scenario = ts::begin(OPERATOR);
        device_registry::init_for_testing(ts::ctx(&mut scenario));
        ts::next_tx(&mut scenario, OPERATOR);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        enrollment::create_policy(&registry, quota, expires, ts::ctx(&mut scenario));
        ts::return_shared(registry);
        ts::next_tx(&mut scenario, OPERATOR);
        let mut policy = ts::take_shared<EnrollmentPolicy>(&scenario);
        enrollment::add_claims(&mut policy, vector[CLAIM, CLAIM_2, CLAIM], ts::ctx(&mut scenario));
        ts::return_shared(policy);
        scenario
    }

    /// `device` enrolls; `sponsor` pays (none = unsponsored); clock at `now`.
    fun enroll(scenario: &mut ts::Scenario, device: address, sponsor: Option<address>, now: u64) {
        // Start the device's transaction, then re-issue it with the sponsor
        // (the builder must inherit the scenario's gas price).
        ts::next_tx(scenario, device);
        if (sponsor.is_some()) {
            let builder = ts::ctx_builder(scenario).set_sponsor(sponsor.destroy_some());
            ts::next_with_context(scenario, builder);
        };
        let mut policy = ts::take_shared<EnrollmentPolicy>(scenario);
        let mut registry = ts::take_shared<DeviceRegistry>(scenario);
        let mut clock = clock::create_for_testing(ts::ctx(scenario));
        clock.set_for_testing(now);
        enrollment::enroll(&mut policy, &mut registry, string::utf8(b"pi"), string::utf8(b"aarch64"), &clock, ts::ctx(scenario));
        clock::destroy_for_testing(clock);
        ts::return_shared(registry);
        ts::return_shared(policy);
    }

    #[test]
    fun sponsored_claim_enrolls_into_custody_with_operator_ownership() {
        let mut scenario = setup(5, 0);
        enroll(&mut scenario, DEVICE, option::some(CLAIM), 1);
        ts::next_tx(&mut scenario, OPERATOR);
        let owner_cap = ts::take_from_address<DeviceOwnerCap>(&scenario, OPERATOR);
        let device_id = device_registry::owner_cap_device_id(&owner_cap);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        assert!(device_registry::is_custodied(&registry, device_id), 0);
        assert!(device_registry::is_active_signer(&registry, device_id, DEVICE), 1);
        let policy = ts::take_shared<EnrollmentPolicy>(&scenario);
        assert!(!enrollment::is_claim(&policy, CLAIM) && enrollment::is_claim(&policy, CLAIM_2), 2);
        assert!(enrollment::remaining(&policy) == 4 && enrollment::enrolled(&policy) == 1, 3);
        ts::return_shared(policy);
        ts::return_shared(registry);
        ts::return_to_address(OPERATOR, owner_cap);
        // The device holds a signer-bound cap for its own key.
        ts::next_tx(&mut scenario, DEVICE);
        let cap = ts::take_from_address<DeviceCap>(&scenario, DEVICE);
        assert!(device_cap::device_id(&cap) == device_id, 4);
        device_cap::assert_signer(&cap, ts::ctx(&mut scenario));
        ts::return_to_address(DEVICE, cap);
        ts::end(scenario);
    }

    #[test]
    fun bulk_claims_enroll_several_devices() {
        let mut scenario = setup(5, 0);
        enroll(&mut scenario, DEVICE, option::some(CLAIM), 1);
        enroll(&mut scenario, DEVICE_2, option::some(CLAIM_2), 1);
        ts::next_tx(&mut scenario, OPERATOR);
        let policy = ts::take_shared<EnrollmentPolicy>(&scenario);
        assert!(enrollment::enrolled(&policy) == 2, 0);
        ts::return_shared(policy);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = enrollment::EUnknownClaim)]
    fun a_claim_is_single_use() {
        let mut scenario = setup(5, 0);
        enroll(&mut scenario, DEVICE, option::some(CLAIM), 1);
        enroll(&mut scenario, DEVICE_2, option::some(CLAIM), 1);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = enrollment::ENotSponsored)]
    fun unsponsored_enrollment_rejected() {
        let mut scenario = setup(5, 0);
        enroll(&mut scenario, DEVICE, option::none(), 1);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = enrollment::EUnknownClaim)]
    fun unknown_sponsor_rejected() {
        let mut scenario = setup(5, 0);
        enroll(&mut scenario, DEVICE, option::some(STRANGER), 1);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = enrollment::ESponsorIsDevice)]
    fun claim_key_cannot_be_the_device_key() {
        let mut scenario = setup(5, 0);
        enroll(&mut scenario, CLAIM, option::some(CLAIM), 1);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = enrollment::EExpired)]
    fun expired_policy_rejected() {
        let mut scenario = setup(5, 100);
        enroll(&mut scenario, DEVICE, option::some(CLAIM), 101);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = enrollment::EQuotaExhausted)]
    fun quota_enforced() {
        let mut scenario = setup(1, 0);
        enroll(&mut scenario, DEVICE, option::some(CLAIM), 1);
        enroll(&mut scenario, DEVICE_2, option::some(CLAIM_2), 1);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = enrollment::EClosed)]
    fun closed_policy_rejected() {
        let mut scenario = setup(5, 0);
        ts::next_tx(&mut scenario, OPERATOR);
        let mut policy = ts::take_shared<EnrollmentPolicy>(&scenario);
        enrollment::close(&mut policy, ts::ctx(&mut scenario));
        ts::return_shared(policy);
        enroll(&mut scenario, DEVICE, option::some(CLAIM), 1);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = enrollment::EUnknownClaim)]
    fun revoked_claim_rejected() {
        let mut scenario = setup(5, 0);
        ts::next_tx(&mut scenario, OPERATOR);
        let mut policy = ts::take_shared<EnrollmentPolicy>(&scenario);
        enrollment::revoke_claim(&mut policy, CLAIM, ts::ctx(&mut scenario));
        ts::return_shared(policy);
        enroll(&mut scenario, DEVICE, option::some(CLAIM), 1);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = enrollment::ENotOperator)]
    fun only_the_operator_adds_claims() {
        let mut scenario = setup(5, 0);
        ts::next_tx(&mut scenario, STRANGER);
        let mut policy = ts::take_shared<EnrollmentPolicy>(&scenario);
        enrollment::add_claims(&mut policy, vector[STRANGER], ts::ctx(&mut scenario));
        ts::return_shared(policy);
        ts::end(scenario);
    }

    // ── Roles ─────────────────────────────────────────────────────────────────

    fun group(scenario: &mut ts::Scenario, admin: address): ID {
        ts::next_tx(scenario, admin);
        let clock = clock::create_for_testing(ts::ctx(scenario));
        device_group::create_group(string::utf8(b"g"), string::utf8(b""), &clock, ts::ctx(scenario));
        clock::destroy_for_testing(clock);
        ts::next_tx(scenario, admin);
        let g = ts::take_shared<DeviceGroup>(scenario);
        let id = device_group::group_id(&g);
        ts::return_shared(g);
        id
    }

    fun grant(scenario: &mut ts::Scenario, sender: address, groups: vector<ID>) {
        ts::next_tx(scenario, sender);
        let mut registry = ts::take_shared<DeviceRegistry>(scenario);
        let mut builder = roles::new_deployer_role(ts::ctx(scenario));
        let mut i = 0;
        while (i < groups.length()) {
            let g = ts::take_shared_by_id<DeviceGroup>(scenario, groups[i]);
            roles::allow_group(&mut builder, &g);
            ts::return_shared(g);
            i = i + 1;
        };
        roles::grant(builder, &mut registry, DELEGATE, ts::ctx(scenario));
        ts::return_shared(registry);
    }

    fun deploy_as(scenario: &mut ts::Scenario, targets: vector<ID>) {
        ts::next_tx(scenario, DELEGATE);
        let role = ts::take_from_address<DeployerRole>(scenario, DELEGATE);
        let registry = ts::take_shared<DeviceRegistry>(scenario);
        let clock = clock::create_for_testing(ts::ctx(scenario));
        roles::create_deployment_as(&role, &registry, string::utf8(b"fw"), string::utf8(b"1.0.0"), string::utf8(b"blob"),
            string::utf8(b"sha"), string::utf8(DIGEST), targets, &clock, ts::ctx(scenario));
        clock::destroy_for_testing(clock);
        ts::return_shared(registry);
        ts::return_to_address(DELEGATE, role);
    }

    #[test]
    fun delegate_deploys_as_the_operator_and_operator_can_delete() {
        let mut scenario = setup(5, 0);
        let g = group(&mut scenario, OPERATOR);
        grant(&mut scenario, OPERATOR, vector[g]);
        deploy_as(&mut scenario, vector[g]);
        ts::next_tx(&mut scenario, OPERATOR);
        let record = ts::take_shared<DeploymentRecord>(&scenario);
        assert!(deployment_manager::creator(&record) == OPERATOR, 0);
        assert!(deployment_manager::delegate(&record) == option::some(DELEGATE), 1);
        assert!(*deployment_manager::target_groups(&record) == vector[g], 2);
        assert!(deployment_manager::target_devices(&record).is_empty(), 3);
        deployment_manager::delete_deployment(record, ts::ctx(&mut scenario));
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = deployment_manager::ENotCreator)]
    fun delegate_cannot_delete() {
        let mut scenario = setup(5, 0);
        let g = group(&mut scenario, OPERATOR);
        grant(&mut scenario, OPERATOR, vector[g]);
        deploy_as(&mut scenario, vector[g]);
        ts::next_tx(&mut scenario, DELEGATE);
        let record = ts::take_shared<DeploymentRecord>(&scenario);
        deployment_manager::delete_deployment(record, ts::ctx(&mut scenario));
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = roles::ERoleRevoked)]
    fun revoked_role_cannot_deploy() {
        let mut scenario = setup(5, 0);
        let g = group(&mut scenario, OPERATOR);
        grant(&mut scenario, OPERATOR, vector[g]);
        ts::next_tx(&mut scenario, DELEGATE);
        let role = ts::take_from_address<DeployerRole>(&scenario, DELEGATE);
        let role_id = object::id(&role);
        ts::return_to_address(DELEGATE, role);
        ts::next_tx(&mut scenario, OPERATOR);
        let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
        roles::revoke(&mut registry, role_id, ts::ctx(&mut scenario));
        ts::return_shared(registry);
        deploy_as(&mut scenario, vector[g]);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = roles::EOutsideScope)]
    fun role_is_limited_to_its_groups() {
        let mut scenario = setup(5, 0);
        let g = group(&mut scenario, OPERATOR);
        let other = group(&mut scenario, OPERATOR);
        grant(&mut scenario, OPERATOR, vector[g]);
        deploy_as(&mut scenario, vector[g, other]);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = roles::ENotGroupAdmin)]
    fun roles_only_cover_the_operators_groups() {
        let mut scenario = setup(5, 0);
        let foreign = group(&mut scenario, STRANGER);
        grant(&mut scenario, OPERATOR, vector[foreign]);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = roles::ENotOperator)]
    fun only_the_operator_revokes() {
        let mut scenario = setup(5, 0);
        let g = group(&mut scenario, OPERATOR);
        grant(&mut scenario, OPERATOR, vector[g]);
        ts::next_tx(&mut scenario, DELEGATE);
        let role = ts::take_from_address<DeployerRole>(&scenario, DELEGATE);
        let role_id = object::id(&role);
        ts::return_to_address(DELEGATE, role);
        ts::next_tx(&mut scenario, STRANGER);
        let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
        roles::revoke(&mut registry, role_id, ts::ctx(&mut scenario));
        ts::return_shared(registry);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = roles::ENoTargets)]
    fun empty_targets_rejected() {
        let mut scenario = setup(5, 0);
        let g = group(&mut scenario, OPERATOR);
        grant(&mut scenario, OPERATOR, vector[g]);
        deploy_as(&mut scenario, vector[]);
        ts::end(scenario);
    }
}
