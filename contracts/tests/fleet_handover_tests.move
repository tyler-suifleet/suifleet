#[test_only]
module sui_edge::fleet_handover_tests {
    use std::string;
    use sui::clock;
    use sui::test_scenario::{Self as ts};
    use sui_edge::device_group::{Self, DeviceGroup};
    use sui_edge::fleet_handover::{Self as handover, Handover};
    use sui_edge::device_registry::{Self, DeviceRegistry, DeviceOwnerCap};
    use sui_edge::deployment_manager::{Self, DeploymentRecord};
    use sui_edge::desired_state;
    use sui_edge::roles::{Self, DeployerRole};
    use sui_edge::enrollment::{Self, EnrollmentPolicy};

    const SOURCE: address = @0xa;
    const RECIPIENT: address = @0xb;
    const STRANGER: address = @0xc;

    #[test]
    fun revoke_delegation_and_close_enrollment_without_reissuing_secrets() {
        let mut s = ts::begin(SOURCE);
        device_registry::init_for_testing(ts::ctx(&mut s));
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        device_group::create_group(string::utf8(b"fleet"), string::utf8(b""), &clock, ts::ctx(&mut s));
        clock::destroy_for_testing(clock);
        ts::next_tx(&mut s, SOURCE);
        let mut registry = ts::take_shared<DeviceRegistry>(&s);
        let group = ts::take_shared<DeviceGroup>(&s);
        let mut builder = roles::new_deployer_role(ts::ctx(&mut s));
        roles::allow_group(&mut builder, &group);
        roles::grant(builder, &mut registry, STRANGER, ts::ctx(&mut s));
        enrollment::create_policy(&registry, 5, 0, ts::ctx(&mut s));
        ts::return_shared(group);
        ts::return_shared(registry);
        ts::next_tx(&mut s, SOURCE);
        let role = ts::take_from_address<DeployerRole>(&s, STRANGER);
        let role_id = object::id(&role);
        let policy = ts::take_shared<EnrollmentPolicy>(&s);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        handover::propose(RECIPIENT, vector[role_id, object::id(&policy)], vector[3, 4], 100,
            &clock, ts::ctx(&mut s));
        ts::return_to_address(STRANGER, role);
        ts::return_shared(policy);
        clock::destroy_for_testing(clock);
        confirm(&mut s, RECIPIENT);
        ts::next_tx(&mut s, SOURCE);
        let mut h = ts::take_shared<Handover>(&s);
        let mut registry = ts::take_shared<DeviceRegistry>(&s);
        let mut policy = ts::take_shared<EnrollmentPolicy>(&s);
        let role = ts::take_from_address<DeployerRole>(&s, STRANGER);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        handover::revoke_role(&mut h, &mut registry, role_id, &clock, ts::ctx(&mut s));
        handover::close_enrollment(&mut h, &mut policy, &clock, ts::ctx(&mut s));
        handover::finish(&mut h, ts::ctx(&mut s));
        assert!(!roles::is_active(&registry, &role), 210);
        assert!(!enrollment::is_open(&policy), 211);
        ts::return_to_address(STRANGER, role);
        ts::return_shared(h);
        ts::return_shared(registry);
        ts::return_shared(policy);
        clock::destroy_for_testing(clock);
        ts::end(s);
    }

    public struct TestBinding has copy, drop, store {}

    #[test]
    #[expected_failure(abort_code = deployment_manager::EPackageOutdated)]
    fun catalog_dependency_authority_must_be_superseded() {
        let mut s = ts::begin(SOURCE);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let mut record = deployment_manager::new_catalog_deployment(string::utf8(b"app"),
            string::utf8(b"1.0.0"), vector[], vector[object::id_from_address(STRANGER)],
            TestBinding {}, true, &clock, ts::ctx(&mut s));
        deployment_manager::handover(&mut record, RECIPIENT, ts::ctx(&mut s));
        deployment_manager::share_deployment(record);
        clock::destroy_for_testing(clock);
        ts::end(s);
    }

    #[test]
    #[expected_failure(abort_code = handover::EExpired)]
    fun expiry_gates_each_step_even_after_confirmation() {
        let mut s = setup(SOURCE);
        confirm(&mut s, RECIPIENT);
        ts::next_tx(&mut s, SOURCE);
        let mut h = ts::take_shared<Handover>(&s);
        let mut group = ts::take_shared<DeviceGroup>(&s);
        let mut clock = clock::create_for_testing(ts::ctx(&mut s));
        clock.set_for_testing(100);
        handover::group(&mut h, &mut group, &clock, ts::ctx(&mut s));
        ts::return_shared(h);
        ts::return_shared(group);
        clock::destroy_for_testing(clock);
        ts::end(s);
    }

    #[test]
    fun populated_fleet_keeps_identity_desired_state_and_controls() {
        let mut s = ts::begin(SOURCE);
        device_registry::init_for_testing(ts::ctx(&mut s));
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        device_group::create_group(string::utf8(b"fleet"), string::utf8(b""), &clock, ts::ctx(&mut s));
        clock::destroy_for_testing(clock);
        ts::next_tx(&mut s, SOURCE);
        let mut registry = ts::take_shared<DeviceRegistry>(&s);
        let mut group = ts::take_shared<DeviceGroup>(&s);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let device = device_registry::register_device(&mut registry, string::utf8(b"edge"), STRANGER,
            string::utf8(b"arm"), &clock, ts::ctx(&mut s));
        let device_id = object::id(&device);
        device_group::add_device(&mut group, &device, ts::ctx(&mut s));
        let cap = device_registry::activate_device(&mut registry, device, ts::ctx(&mut s));
        let cap_id = object::id(&cap);
        let mut record = deployment_manager::new_deployment_verified(string::utf8(b"app"),
            string::utf8(b"1.0.0"), string::utf8(b"blob"), string::utf8(b"digest"),
            string::utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            vector[object::id(&group)], vector[], &clock, ts::ctx(&mut s));
        deployment_manager::set_control(&mut record, 1, 0, 0, 0, 0, 0, ts::ctx(&mut s));
        let record_id = object::id(&record);
        let mut plan = desired_state::begin(&group, ts::ctx(&mut s));
        desired_state::set_component(&mut plan, &mut record);
        desired_state::commit(plan, &mut group, &clock, ts::ctx(&mut s));
        handover::propose(RECIPIENT, vector[cap_id, object::id(&group), record_id],
            vector[0, 1, 2], 100, &clock, ts::ctx(&mut s));
        transfer::public_transfer(cap, SOURCE);
        deployment_manager::share_deployment(record);
        ts::return_shared(group);
        ts::return_shared(registry);
        clock::destroy_for_testing(clock);
        confirm(&mut s, RECIPIENT);
        // A crash between these transactions leaves explicit progress; the
        // source cannot use the transferred cap, and can resume remaining work.
        ts::next_tx(&mut s, SOURCE);
        let mut h = ts::take_shared<Handover>(&s);
        let cap = ts::take_from_sender<DeviceOwnerCap>(&s);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        handover::device(&mut h, cap, &clock, ts::ctx(&mut s));
        ts::return_shared(h);
        clock::destroy_for_testing(clock);
        ts::next_tx(&mut s, SOURCE);
        let mut h = ts::take_shared<Handover>(&s);
        let mut group = ts::take_shared<DeviceGroup>(&s);
        let mut record = ts::take_shared<DeploymentRecord>(&s);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        handover::group(&mut h, &mut group, &clock, ts::ctx(&mut s));
        handover::deployment(&mut h, &mut record, &clock, ts::ctx(&mut s));
        handover::finish(&mut h, ts::ctx(&mut s));
        ts::return_shared(h);
        ts::return_shared(group);
        ts::return_shared(record);
        clock::destroy_for_testing(clock);
        ts::next_tx(&mut s, RECIPIENT);
        let cap = ts::take_from_sender<DeviceOwnerCap>(&s);
        let registry = ts::take_shared<DeviceRegistry>(&s);
        let mut group = ts::take_shared<DeviceGroup>(&s);
        let mut record = ts::take_shared<DeploymentRecord>(&s);
        assert!(device_registry::is_active_signer(&registry, device_id, STRANGER), 200);
        assert!(device_registry::owner_cap_device_id(&cap) == device_id, 201);
        assert!(object::id(&cap) == cap_id, 202);
        assert!(device_group::contains_device(&group, device_id), 203);
        assert!(desired_state::revision(&group) == 1, 204);
        assert!(*desired_state::components(&group).get(&string::utf8(b"app")) == record_id, 205);
        assert!(deployment_manager::creator(&record) == RECIPIENT, 206);
        // Recipient can revise and control the original records.
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let mut plan = desired_state::begin(&group, ts::ctx(&mut s));
        desired_state::set_component(&mut plan, &mut record);
        desired_state::commit(plan, &mut group, &clock, ts::ctx(&mut s));
        deployment_manager::set_control(&mut record, 0, 0, 0, 0, 0, 0, ts::ctx(&mut s));
        clock::destroy_for_testing(clock);
        ts::return_to_sender(&s, cap);
        ts::return_shared(registry);
        ts::return_shared(group);
        ts::return_shared(record);
        ts::end(s);
    }

    fun setup(admin: address): ts::Scenario {
        let mut s = ts::begin(admin);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        device_group::create_group(string::utf8(b"fleet"), string::utf8(b""), &clock, ts::ctx(&mut s));
        clock::destroy_for_testing(clock);
        ts::next_tx(&mut s, SOURCE);
        let group = ts::take_shared<DeviceGroup>(&s);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        handover::propose(RECIPIENT, vector[object::id(&group)], vector[1], 100, &clock, ts::ctx(&mut s));
        ts::return_shared(group);
        clock::destroy_for_testing(clock);
        s
    }

    fun confirm(s: &mut ts::Scenario, sender: address) {
        ts::next_tx(s, sender);
        let mut h = ts::take_shared<Handover>(s);
        let clock = clock::create_for_testing(ts::ctx(s));
        handover::confirm(&mut h, &clock, ts::ctx(s));
        ts::return_shared(h);
        clock::destroy_for_testing(clock);
    }

    fun migrate(s: &mut ts::Scenario) {
        ts::next_tx(s, SOURCE);
        let mut h = ts::take_shared<Handover>(s);
        let mut group = ts::take_shared<DeviceGroup>(s);
        let clock = clock::create_for_testing(ts::ctx(s));
        handover::group(&mut h, &mut group, &clock, ts::ctx(s));
        assert!(device_group::admin(&group) == RECIPIENT, 100);
        assert!(handover::completed(&h) == vector[true], 101);
        ts::return_shared(h);
        ts::return_shared(group);
        clock::destroy_for_testing(clock);
    }

    #[test]
    fun confirmed_transfer_resumes_and_finishes_in_separate_transactions() {
        let mut s = setup(SOURCE);
        confirm(&mut s, RECIPIENT);
        migrate(&mut s);
        ts::next_tx(&mut s, RECIPIENT);
        let mut h = ts::take_shared<Handover>(&s);
        handover::finish(&mut h, ts::ctx(&mut s));
        assert!(handover::state(&h) == 3, 102);
        ts::return_shared(h);
        ts::end(s);
    }

    #[test]
    #[expected_failure(abort_code = handover::EParty)]
    fun source_cannot_confirm_for_recipient() {
        let mut s = setup(SOURCE);
        confirm(&mut s, SOURCE);
        ts::end(s);
    }

    #[test]
    #[expected_failure(abort_code = handover::EState)]
    fun transfer_requires_recipient_confirmation() {
        let mut s = setup(SOURCE);
        migrate(&mut s);
        ts::end(s);
    }

    #[test]
    #[expected_failure(abort_code = device_group::ENotAdmin)]
    fun cannot_transfer_another_tenants_group() {
        let mut s = setup(STRANGER);
        confirm(&mut s, RECIPIENT);
        migrate(&mut s);
        ts::end(s);
    }

    #[test]
    #[expected_failure(abort_code = handover::EInventory)]
    fun completed_step_cannot_be_replayed() {
        let mut s = setup(SOURCE);
        confirm(&mut s, RECIPIENT);
        migrate(&mut s);
        migrate(&mut s);
        ts::end(s);
    }

    #[test]
    #[expected_failure(abort_code = handover::EInventory)]
    fun cannot_finish_with_pending_items() {
        let mut s = setup(SOURCE);
        confirm(&mut s, RECIPIENT);
        ts::next_tx(&mut s, RECIPIENT);
        let mut h = ts::take_shared<Handover>(&s);
        handover::finish(&mut h, ts::ctx(&mut s));
        ts::return_shared(h);
        ts::end(s);
    }

    #[test]
    #[expected_failure(abort_code = handover::EState)]
    fun recipient_can_stop_further_steps() {
        let mut s = setup(SOURCE);
        confirm(&mut s, RECIPIENT);
        ts::next_tx(&mut s, RECIPIENT);
        let mut h = ts::take_shared<Handover>(&s);
        handover::cancel(&mut h, ts::ctx(&mut s));
        ts::return_shared(h);
        migrate(&mut s);
        ts::end(s);
    }

    #[test]
    #[expected_failure(abort_code = device_group::ENotAdmin)]
    fun old_owner_cannot_delete_migrated_group() {
        let mut s = setup(SOURCE);
        confirm(&mut s, RECIPIENT);
        migrate(&mut s);
        ts::next_tx(&mut s, SOURCE);
        let group = ts::take_shared<DeviceGroup>(&s);
        device_group::delete_group(group, ts::ctx(&mut s));
        ts::end(s);
    }
}
