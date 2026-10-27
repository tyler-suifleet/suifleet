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

    // ── SF-129 retirement ───────────────────────────────────────────────────

    /// Retire `record` after confirming `groups`, as `sender`; returns whether
    /// the record object was deleted by the transaction.
    fun retire(scenario: &mut ts::Scenario, sender: address, record: ID, groups: vector<ID>): bool {
        ts::next_tx(scenario, sender);
        let deployment = ts::take_shared_by_id<DeploymentRecord>(scenario, record);
        let mut retirement = desired_state::begin_retirement(&deployment, ts::ctx(scenario));
        groups.do!(|id| {
            let mut group = ts::take_shared_by_id<DeviceGroup>(scenario, id);
            desired_state::confirm_not_desired(&mut retirement, &mut group, ts::ctx(scenario));
            ts::return_shared(group);
        });
        desired_state::retire(retirement, deployment, ts::ctx(scenario));
        ts::next_tx(scenario, sender).deleted().contains(&record)
    }

    #[test]
    fun superseded_managed_record_is_retired_after_checking_its_groups() {
        let mut scenario = ts::begin(ADMIN);
        let a = new_group(&mut scenario, ADMIN);
        let b = new_group(&mut scenario, ADMIN);
        let fw1 = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[a, b, a]);
        revise(&mut scenario, ADMIN, a, vector[fw1], vector[]);
        revise(&mut scenario, ADMIN, b, vector[fw1], vector[]);
        let fw2 = deploy(&mut scenario, ADMIN, b"firmware", b"1.1.0", vector[a, b]);
        revise(&mut scenario, ADMIN, a, vector[fw2], vector[]);
        revise(&mut scenario, ADMIN, b, vector[], vector[b"firmware"]);
        assert!(retire(&mut scenario, ADMIN, fw1, vector[a, b]), 0);
        ts::end(scenario);
    }

    #[test]
    fun unmanaged_record_needs_no_group_check() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        let once = deploy(&mut scenario, ADMIN, b"agent", b"1.0.0", vector[group]);
        assert!(retire(&mut scenario, ADMIN, once, vector[]), 0);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = desired_state::EStillDesired)]
    fun desired_record_cannot_be_retired() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        let fw = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[group]);
        revise(&mut scenario, ADMIN, group, vector[fw], vector[]);
        retire(&mut scenario, ADMIN, fw, vector[group]);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = desired_state::EUnconfirmed)]
    fun every_target_group_must_be_confirmed() {
        let mut scenario = ts::begin(ADMIN);
        let a = new_group(&mut scenario, ADMIN);
        let b = new_group(&mut scenario, ADMIN);
        let fw = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[a, b]);
        revise(&mut scenario, ADMIN, b, vector[fw], vector[]);
        // Group b still desires it; skipping b must not delete the record.
        retire(&mut scenario, ADMIN, fw, vector[a]);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = desired_state::EWrongGroup)]
    fun untargeted_group_does_not_confirm() {
        let mut scenario = ts::begin(ADMIN);
        let a = new_group(&mut scenario, ADMIN);
        let other = new_group(&mut scenario, ADMIN);
        let fw = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[a]);
        revise(&mut scenario, ADMIN, a, vector[fw], vector[]);
        retire(&mut scenario, ADMIN, fw, vector[other]);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = desired_state::ENotCreator)]
    fun only_the_creator_retires() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        let fw = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[group]);
        retire(&mut scenario, OTHER, fw, vector[]);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = desired_state::EWrongRecord)]
    fun proof_is_bound_to_its_record() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        let checked = deploy(&mut scenario, ADMIN, b"agent", b"1.0.0", vector[group]);
        let desired = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[group]);
        revise(&mut scenario, ADMIN, group, vector[desired], vector[]);
        ts::next_tx(&mut scenario, ADMIN);
        let first = ts::take_shared_by_id<DeploymentRecord>(&scenario, checked);
        let second = ts::take_shared_by_id<DeploymentRecord>(&scenario, desired);
        let retirement = desired_state::begin_retirement(&first, ts::ctx(&mut scenario));
        desired_state::retire(retirement, second, ts::ctx(&mut scenario));
        abort 0
    }

    // A confirmed group cannot make the record desired again before `retire`.
    #[test, expected_failure(abort_code = desired_state::ERetiring)]
    fun confirmed_group_cannot_reassign_the_record_in_the_same_transaction() {
        let mut scenario = ts::begin(ADMIN);
        let group_id = new_group(&mut scenario, ADMIN);
        let fw = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[group_id]);
        revise(&mut scenario, ADMIN, group_id, vector[fw], vector[]);
        revise(&mut scenario, ADMIN, group_id, vector[], vector[b"firmware"]);
        ts::next_tx(&mut scenario, ADMIN);
        let mut deployment = ts::take_shared_by_id<DeploymentRecord>(&scenario, fw);
        let mut group = ts::take_shared_by_id<DeviceGroup>(&scenario, group_id);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        let mut retirement = desired_state::begin_retirement(&deployment, ts::ctx(&mut scenario));
        desired_state::confirm_not_desired(&mut retirement, &mut group, ts::ctx(&mut scenario));
        let mut plan = desired_state::begin(&group, ts::ctx(&mut scenario));
        desired_state::set_component(&mut plan, &mut deployment);
        desired_state::commit(plan, &mut group, &clock, ts::ctx(&mut scenario));
        desired_state::retire(retirement, deployment, ts::ctx(&mut scenario));
        abort 0
    }

    // A plan prepared before `begin_retirement` is refused at commit too.
    #[test, expected_failure(abort_code = desired_state::ERetiring)]
    fun plan_prepared_before_retirement_cannot_commit_the_record() {
        let mut scenario = ts::begin(ADMIN);
        let group_id = new_group(&mut scenario, ADMIN);
        let fw = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[group_id]);
        ts::next_tx(&mut scenario, ADMIN);
        let mut deployment = ts::take_shared_by_id<DeploymentRecord>(&scenario, fw);
        let mut group = ts::take_shared_by_id<DeviceGroup>(&scenario, group_id);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        let mut plan = desired_state::begin(&group, ts::ctx(&mut scenario));
        desired_state::set_component(&mut plan, &mut deployment);
        let mut retirement = desired_state::begin_retirement(&deployment, ts::ctx(&mut scenario));
        desired_state::confirm_not_desired(&mut retirement, &mut group, ts::ctx(&mut scenario));
        desired_state::commit(plan, &mut group, &clock, ts::ctx(&mut scenario));
        desired_state::retire(retirement, deployment, ts::ctx(&mut scenario));
        abort 0
    }

    // An unmanaged record assigned after `begin_retirement` was never checked.
    #[test, expected_failure(abort_code = desired_state::ERetiring)]
    fun record_assigned_after_begin_retirement_is_not_retired() {
        let mut scenario = ts::begin(ADMIN);
        let group_id = new_group(&mut scenario, ADMIN);
        let fw = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[group_id]);
        ts::next_tx(&mut scenario, ADMIN);
        let mut deployment = ts::take_shared_by_id<DeploymentRecord>(&scenario, fw);
        let mut group = ts::take_shared_by_id<DeviceGroup>(&scenario, group_id);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        let retirement = desired_state::begin_retirement(&deployment, ts::ctx(&mut scenario));
        let mut plan = desired_state::begin(&group, ts::ctx(&mut scenario));
        desired_state::set_component(&mut plan, &mut deployment);
        desired_state::commit(plan, &mut group, &clock, ts::ctx(&mut scenario));
        desired_state::retire(retirement, deployment, ts::ctx(&mut scenario));
        abort 0
    }

    // The check binds only its own transaction and records: the same
    // transaction may commit other components, and later revisions proceed.
    #[test]
    fun retirement_check_does_not_block_other_revisions() {
        let mut scenario = ts::begin(ADMIN);
        let group_id = new_group(&mut scenario, ADMIN);
        let fw1 = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[group_id]);
        revise(&mut scenario, ADMIN, group_id, vector[fw1], vector[]);
        let fw2 = deploy(&mut scenario, ADMIN, b"firmware", b"1.1.0", vector[group_id]);
        let fw3 = deploy(&mut scenario, ADMIN, b"firmware", b"1.2.0", vector[group_id]);
        ts::next_tx(&mut scenario, ADMIN);
        let deployment = ts::take_shared_by_id<DeploymentRecord>(&scenario, fw1);
        let mut next = ts::take_shared_by_id<DeploymentRecord>(&scenario, fw2);
        let mut group = ts::take_shared_by_id<DeviceGroup>(&scenario, group_id);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        let mut plan = desired_state::begin(&group, ts::ctx(&mut scenario));
        desired_state::set_component(&mut plan, &mut next);
        desired_state::commit(plan, &mut group, &clock, ts::ctx(&mut scenario));
        let mut retirement = desired_state::begin_retirement(&deployment, ts::ctx(&mut scenario));
        desired_state::confirm_not_desired(&mut retirement, &mut group, ts::ctx(&mut scenario));
        desired_state::retire(retirement, deployment, ts::ctx(&mut scenario));
        let mut plan = desired_state::begin(&group, ts::ctx(&mut scenario));
        desired_state::remove_component(&mut plan, string::utf8(b"firmware"));
        desired_state::commit(plan, &mut group, &clock, ts::ctx(&mut scenario));
        clock::destroy_for_testing(clock);
        ts::return_shared(next);
        ts::return_shared(group);
        assert!(ts::next_tx(&mut scenario, ADMIN).deleted().contains(&fw1), 0);
        revise(&mut scenario, ADMIN, group_id, vector[fw3], vector[]);
        assert!(desired(&mut scenario, group_id, b"firmware") == option::some(fw3), 1);
        assert!(revision(&mut scenario, group_id) == 4, 2);
        ts::end(scenario);
    }

    #[test]
    fun catalog_and_rollout_records_use_their_deletion_paths() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        let (rollout_id, bound) = rollout_and_catalog(&mut scenario, group);
        assert!(retire(&mut scenario, ADMIN, rollout_id, vector[]), 0);
        assert!(retire(&mut scenario, ADMIN, bound, vector[]), 1);
        ts::end(scenario);
    }

    /// Move `record` to sharded receipts, then retire every (empty) root.
    fun shard_and_drain(scenario: &mut ts::Scenario, record: ID, catalog: bool, drain: bool) {
        ts::next_tx(scenario, ADMIN);
        let mut d = ts::take_shared_by_id<DeploymentRecord>(scenario, record);
        if (catalog) sui_edge::component_catalog::begin_sharded_receipts(&mut d, ts::ctx(scenario))
        else deployment_manager::begin_sharded_receipts(&mut d, ts::ctx(scenario));
        assert!(deployment_manager::schema_version(&d) != 3, 0);
        ts::return_shared(d);
        if (!drain) return;
        ts::next_tx(scenario, ADMIN);
        let mut d = ts::take_shared_by_id<DeploymentRecord>(scenario, record);
        deployment_manager::begin_receipt_cleanup(&mut d, ts::ctx(scenario));
        let roots = *deployment_manager::receipt_roots(&d);
        roots.do!(|root| deployment_manager::retire_receipt_shard(&mut d,
            ts::take_shared_by_id<deployment_manager::ReceiptShard>(scenario, root)));
        ts::return_shared(d);
    }

    /// Two transaction-local records: a staged rollout and a catalog-bound one.
    fun rollout_and_catalog(scenario: &mut ts::Scenario, group: ID): (ID, ID) {
        ts::next_tx(scenario, ADMIN);
        let clock = clock::create_for_testing(ts::ctx(scenario));
        let policy = deployment_manager::rollout_policy(1, 1, 1000, 100, 1, 1, 2000);
        let rollout = deployment_manager::new_rollout_verified(string::utf8(b"app"), string::utf8(b"1"),
            string::utf8(b"recipe"), string::utf8(b"hash"), string::utf8(DIGEST), vector[group],
            vector[object::id_from_address(@0x1)], vector[0], policy, &clock, ts::ctx(scenario));
        let rollout_id = object::id(&rollout);
        deployment_manager::share_deployment(rollout);
        sui_edge::component_catalog::create_catalog(ts::ctx(scenario));
        ts::next_tx(scenario, ADMIN);
        let mut catalog = ts::take_shared<sui_edge::component_catalog::ComponentCatalog>(scenario);
        sui_edge::component_catalog::publish_version(&mut catalog, string::utf8(b"app"), string::utf8(b"1.0.0"),
            vector[string::utf8(b"linux")], vector[string::utf8(b"x86_64")], vector[string::utf8(b"recipe")],
            vector[string::utf8(DIGEST)], vector[string::utf8(DIGEST)], &clock, ts::ctx(scenario));
        ts::return_shared(catalog);
        ts::next_tx(scenario, ADMIN);
        let version = ts::take_immutable<sui_edge::component_catalog::ComponentVersion>(scenario);
        sui_edge::component_catalog::create_deployment(&version, vector[group], vector[], &clock, ts::ctx(scenario));
        ts::return_immutable(version);
        let effects = ts::next_tx(scenario, ADMIN);
        let bound = *effects.shared().find_index!(|id| *id != rollout_id && *id != group).map!(|i| effects.shared()[i]).borrow();
        clock::destroy_for_testing(clock);
        (rollout_id, bound)
    }

    // SF-127 sharding rewrites the inline schema; retire follows the
    // directory's original schema once the receipt roots are retired.
    #[test]
    fun sharded_records_use_their_original_deletion_paths() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        let (rollout_id, bound) = rollout_and_catalog(&mut scenario, group);
        let plain = deploy(&mut scenario, ADMIN, b"firmware", b"1.0.0", vector[group]);
        shard_and_drain(&mut scenario, rollout_id, false, true);
        shard_and_drain(&mut scenario, bound, true, true);
        shard_and_drain(&mut scenario, plain, false, true);
        assert!(retire(&mut scenario, ADMIN, rollout_id, vector[]), 0);
        assert!(retire(&mut scenario, ADMIN, bound, vector[]), 1);
        assert!(retire(&mut scenario, ADMIN, plain, vector[]), 2);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 17, location = sui_edge::deployment_manager)]
    fun sharded_record_with_live_roots_is_not_retired() {
        let mut scenario = ts::begin(ADMIN);
        let group = new_group(&mut scenario, ADMIN);
        let (rollout_id, _) = rollout_and_catalog(&mut scenario, group);
        shard_and_drain(&mut scenario, rollout_id, false, false);
        retire(&mut scenario, ADMIN, rollout_id, vector[]);
        ts::end(scenario);
    }
}
