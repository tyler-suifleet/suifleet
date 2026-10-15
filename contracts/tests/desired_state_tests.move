#[test_only]
module sui_edge::desired_state_tests {
    use std::string;
    use sui::clock;
    use sui::test_scenario::{Self as ts};
    use sui_edge::deployment_manager::{Self, DeploymentRecord};
    use sui_edge::desired_state;
    use sui_edge::device_group::{Self, DeviceGroup};

    const ADMIN: address = @0xAD;
    const OTHER: address = @0xBE;
    const DIGEST: vector<u8> = b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fun new_group(scenario: &mut ts::Scenario, admin: address): ID {
        ts::next_tx(scenario, admin);
        let clock = clock::create_for_testing(ts::ctx(scenario));
        device_group::create_group(string::utf8(b"g"), string::utf8(b""), &clock, ts::ctx(scenario));
        clock::destroy_for_testing(clock);
        ts::next_tx(scenario, admin);
        let group = ts::take_shared<DeviceGroup>(scenario);
        let id = device_group::group_id(&group);
        ts::return_shared(group);
        id
    }

    /// A verified deployment of `name`@`version` targeting `groups`, shared by `sender`.
    fun deploy(scenario: &mut ts::Scenario, sender: address, name: vector<u8>, version: vector<u8>, groups: vector<ID>): ID {
        ts::next_tx(scenario, sender);
        let clock = clock::create_for_testing(ts::ctx(scenario));
        let record = deployment_manager::new_deployment_verified(
            string::utf8(name), string::utf8(version), string::utf8(b"blob"), string::utf8(b"sha"),
            string::utf8(DIGEST), groups, vector[], &clock, ts::ctx(scenario));
        let id = object::id(&record);
        deployment_manager::share_deployment(record);
        clock::destroy_for_testing(clock);
        id
    }

    /// One revision setting `deployments` and removing `removed`, by `sender`.
    fun revise(scenario: &mut ts::Scenario, sender: address, group_id: ID, deployments: vector<ID>, removed: vector<vector<u8>>) {
        ts::next_tx(scenario, sender);
        let mut group = ts::take_shared_by_id<DeviceGroup>(scenario, group_id);
        let clock = clock::create_for_testing(ts::ctx(scenario));
        let mut plan = desired_state::begin(&group, ts::ctx(scenario));
        let mut i = 0;
        while (i < deployments.length()) {
            let mut record = ts::take_shared_by_id<DeploymentRecord>(scenario, deployments[i]);
            desired_state::set_component(&mut plan, &mut record);
            ts::return_shared(record);
            i = i + 1;
        };
        let mut j = 0;
        while (j < removed.length()) {
            desired_state::remove_component(&mut plan, string::utf8(removed[j]));
            j = j + 1;
        };
        desired_state::commit(plan, &mut group, &clock, ts::ctx(scenario));
        clock::destroy_for_testing(clock);
        ts::return_shared(group);
    }

    fun desired(scenario: &mut ts::Scenario, group_id: ID, name: vector<u8>): Option<ID> {
        ts::next_tx(scenario, ADMIN);
        let group = ts::take_shared_by_id<DeviceGroup>(scenario, group_id);
        let components = desired_state::components(&group);
        let key = string::utf8(name);
        let result = if (components.contains(&key)) { option::some(*components.get(&key)) } else { option::none() };
        ts::return_shared(group);
        result
    }

    fun revision(scenario: &mut ts::Scenario, group_id: ID): u64 {
        ts::next_tx(scenario, ADMIN);
        let group = ts::take_shared_by_id<DeviceGroup>(scenario, group_id);
        let revision = desired_state::revision(&group);
        ts::return_shared(group);
        revision
    }

    #[test]
    fun multi_component_revision_then_supersession_and_removal() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        assert!(revision(&mut scenario, group) == 0, 0);
        let fw1 = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[group]);
        let agent = deploy(&mut scenario, ADMIN, b"agent", b"2.0.0", vector[group]);
        // Both components become desired in one revision.
        revise(&mut scenario, ADMIN, group, vector[fw1, agent], vector[]);
        assert!(revision(&mut scenario, group) == 1, 1);
        assert!(desired(&mut scenario, group, b"firmware") == option::some(fw1), 2);
        assert!(desired(&mut scenario, group, b"agent") == option::some(agent), 3);
        // A newer firmware replaces the older one; agent is untouched.
        let fw2 = deploy(&mut scenario, ADMIN, b"firmware", b"1.1.0", vector[group]);
        revise(&mut scenario, ADMIN, group, vector[fw2], vector[]);
        assert!(revision(&mut scenario, group) == 2, 4);
        assert!(desired(&mut scenario, group, b"firmware") == option::some(fw2), 5);
        assert!(desired(&mut scenario, group, b"agent") == option::some(agent), 6);
        // Removing a component is its own revision.
        revise(&mut scenario, ADMIN, group, vector[], vector[b"agent"]);
        assert!(revision(&mut scenario, group) == 3, 7);
        assert!(desired(&mut scenario, group, b"agent") == option::none(), 8);
        // Referenced records are marked managed; the one-shot path skips them.
        ts::next_tx(&mut scenario, ADMIN);
        let record = ts::take_shared_by_id<DeploymentRecord>(&scenario, fw1);
        assert!(deployment_manager::is_managed(&record), 9);
        ts::return_shared(record);
        ts::end(scenario);
    }

    #[test]
    fun create_and_assign_in_one_transaction() {
        let mut scenario = ts::begin(ADMIN);
        let group_id = new_group(&mut scenario, ADMIN);
        ts::next_tx(&mut scenario, ADMIN);
        let mut group = ts::take_shared_by_id<DeviceGroup>(&scenario, group_id);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        let mut record = deployment_manager::new_deployment_verified(
            string::utf8(b"firmware"), string::utf8(b"1.0.0"), string::utf8(b"blob"), string::utf8(b"sha"),
            string::utf8(DIGEST), vector[group_id], vector[], &clock, ts::ctx(&mut scenario));
        let mut plan = desired_state::begin(&group, ts::ctx(&mut scenario));
        desired_state::set_component(&mut plan, &mut record);
        desired_state::commit(plan, &mut group, &clock, ts::ctx(&mut scenario));
        assert!(deployment_manager::is_managed(&record), 0);
        let id = object::id(&record);
        deployment_manager::share_deployment(record);
        clock::destroy_for_testing(clock);
        ts::return_shared(group);
        assert!(desired(&mut scenario, group_id, b"firmware") == option::some(id), 1);
        ts::end(scenario);
    }

    #[test]
    fun deleting_a_managed_record_cleans_its_marker() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        let fw = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[group]);
        revise(&mut scenario, ADMIN, group, vector[fw], vector[]);
        ts::next_tx(&mut scenario, ADMIN);
        let record = ts::take_shared_by_id<DeploymentRecord>(&scenario, fw);
        deployment_manager::delete_deployment(record, ts::ctx(&mut scenario));
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = desired_state::ENotAdmin)]
    fun only_the_admin_begins_a_revision() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        let fw = deploy(&mut scenario, OTHER, b"firmware", b"1.0.0", vector[group]);
        revise(&mut scenario, OTHER, group, vector[fw], vector[]);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = desired_state::ENotCreator)]
    fun another_operators_deployment_rejected() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        let foreign = deploy(&mut scenario, OTHER, b"firmware", b"6.6.6", vector[group]);
        revise(&mut scenario, ADMIN, group, vector[foreign], vector[]);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = desired_state::ENotTargeted)]
    fun deployment_must_target_the_group() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        let other_group = new_group(&mut scenario, ADMIN);
        let fw = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[other_group]);
        revise(&mut scenario, ADMIN, group, vector[fw], vector[]);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = desired_state::EUnverifiedRecipe)]
    fun unverified_recipe_rejected() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        ts::next_tx(&mut scenario, ADMIN);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        deployment_manager::create_deployment(string::utf8(b"firmware"), string::utf8(b"1.0.0"),
            string::utf8(b"blob"), string::utf8(b"sha"), vector[group], vector[], &clock, ts::ctx(&mut scenario));
        clock::destroy_for_testing(clock);
        ts::next_tx(&mut scenario, ADMIN);
        let record = ts::take_shared<DeploymentRecord>(&scenario);
        let id = object::id(&record);
        ts::return_shared(record);
        revise(&mut scenario, ADMIN, group, vector[id], vector[]);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = desired_state::EWrongGroup)]
    fun plan_cannot_be_committed_to_another_group() {
        let mut scenario = ts::begin(ADMIN);
        let first = new_group(&mut scenario, ADMIN);
        let second = new_group(&mut scenario, ADMIN);
        ts::next_tx(&mut scenario, ADMIN);
        let a = ts::take_shared_by_id<DeviceGroup>(&scenario, first);
        let mut b = ts::take_shared_by_id<DeviceGroup>(&scenario, second);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        let plan = desired_state::begin(&a, ts::ctx(&mut scenario));
        desired_state::commit(plan, &mut b, &clock, ts::ctx(&mut scenario));
        clock::destroy_for_testing(clock);
        ts::return_shared(a);
        ts::return_shared(b);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = desired_state::EStaleRevision)]
    fun concurrent_plans_cannot_both_commit() {
        let mut scenario = ts::begin(ADMIN);
        let group_id = new_group(&mut scenario, ADMIN);
        ts::next_tx(&mut scenario, ADMIN);
        let mut group = ts::take_shared_by_id<DeviceGroup>(&scenario, group_id);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        let first = desired_state::begin(&group, ts::ctx(&mut scenario));
        let second = desired_state::begin(&group, ts::ctx(&mut scenario));
        desired_state::commit(first, &mut group, &clock, ts::ctx(&mut scenario));
        // Built on revision 0, which is no longer current.
        desired_state::commit(second, &mut group, &clock, ts::ctx(&mut scenario));
        clock::destroy_for_testing(clock);
        ts::return_shared(group);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = desired_state::EUnknownComponent)]
    fun removing_an_absent_component_rejected() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        revise(&mut scenario, ADMIN, group, vector[], vector[b"missing"]);
        ts::end(scenario);
    }
}
