#[test_only]
module sui_edge::seal_policy_tests {
    use std::string;
    use sui::bcs;
    use sui::clock;
    use sui::test_scenario::{Self as ts};
    use sui_edge::device_group::{Self, DeviceGroup};
    use sui_edge::device_registry::{Self, DeviceRegistry, DeviceOwnerCap};
    use sui_edge::seal_policy::{Self, AccessPolicy, AccessAdminCap};

    const ADMIN: address = @0xA1;
    const READER: address = @0xB1;
    const DEVICE: address = @0xD1;
    const DEVICE_NEW_KEY: address = @0xD2;
    const STRANGER: address = @0xBAD;
    const NONCE: vector<u8> = x"000102030405060708090a0b0c0d0e0f";

    fun setup(): ts::Scenario {
        let mut scenario = ts::begin(ADMIN);
        device_registry::init_for_testing(ts::ctx(&mut scenario));
        ts::next_tx(&mut scenario, ADMIN);
        let cap = seal_policy::create_policy(string::utf8(b"releases"), ts::ctx(&mut scenario));
        transfer::public_transfer(cap, ADMIN);
        ts::next_tx(&mut scenario, ADMIN);
        scenario
    }

    /// A current identity of `policy` with the given nonce.
    fun identity(policy: &AccessPolicy, nonce: vector<u8>): vector<u8> {
        let mut id = seal_policy::identity_prefix(policy);
        id.append(nonce);
        id
    }

    /// Register and activate a device whose key is `signer`; returns its ID.
    fun activated_device(scenario: &mut ts::Scenario, signer: address): ID {
        ts::next_tx(scenario, ADMIN);
        let mut registry = ts::take_shared<DeviceRegistry>(scenario);
        let clock = clock::create_for_testing(ts::ctx(scenario));
        let device = device_registry::register_device(&mut registry, string::utf8(b"pi"), signer, string::utf8(b"aarch64"), &clock, ts::ctx(scenario));
        let device_id = device_registry::device_id(&device);
        let owner = device_registry::activate_device(&mut registry, device, ts::ctx(scenario));
        transfer::public_transfer(owner, ADMIN);
        clock::destroy_for_testing(clock);
        ts::return_shared(registry);
        ts::next_tx(scenario, ADMIN);
        device_id
    }

    #[test]
    fun accounts_are_granted_and_revoked_for_current_identities_only() {
        let mut scenario = setup();
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        let id = identity(&policy, NONCE);
        assert!(!seal_policy::account_allowed(&policy, &id, READER), 0);
        seal_policy::grant_account(&mut policy, &cap, READER);
        assert!(seal_policy::account_allowed(&policy, &id, READER), 1);
        assert!(!seal_policy::account_allowed(&policy, &id, STRANGER), 2);
        // Wrong prefix, short and long nonces are not this policy's identities.
        let mut other = id;
        *&mut other[0] = other[0] ^ 1;
        assert!(!seal_policy::account_allowed(&policy, &other, READER), 3);
        assert!(!seal_policy::account_allowed(&policy, &identity(&policy, x"00"), READER), 4);
        let mut long = NONCE;
        long.append(NONCE); long.append(NONCE); long.append(NONCE); long.push_back(0);
        assert!(!seal_policy::account_allowed(&policy, &identity(&policy, long), READER), 5);
        assert!(!seal_policy::account_allowed(&policy, &seal_policy::identity_prefix(&policy), READER), 6);
        seal_policy::revoke_account(&mut policy, &cap, READER);
        assert!(!seal_policy::account_allowed(&policy, &id, READER), 7);
        ts::return_shared(policy);
        ts::return_to_sender(&scenario, cap);
        ts::end(scenario);
    }

    #[test]
    fun identity_layout_is_policy_id_then_little_endian_epoch() {
        let mut scenario = setup();
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        seal_policy::rotate_key_epoch(&mut policy, &cap);
        seal_policy::rotate_key_epoch(&mut policy, &cap);
        let mut expected = object::id(&policy).to_bytes();
        expected.append(x"0200000000000000");
        assert!(seal_policy::identity_prefix(&policy) == expected, 0);
        assert!(bcs::to_bytes(&seal_policy::key_epoch(&policy)) == x"0200000000000000", 1);
        ts::return_shared(policy);
        ts::return_to_sender(&scenario, cap);
        ts::end(scenario);
    }

    #[test]
    fun rotation_retires_every_earlier_identity() {
        let mut scenario = setup();
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        seal_policy::grant_account(&mut policy, &cap, READER);
        let old = identity(&policy, NONCE);
        seal_policy::rotate_key_epoch(&mut policy, &cap);
        assert!(seal_policy::key_epoch(&policy) == 1, 0);
        assert!(!seal_policy::account_allowed(&policy, &old, READER), 1);
        assert!(seal_policy::account_allowed(&policy, &identity(&policy, NONCE), READER), 2);
        ts::return_shared(policy);
        ts::return_to_sender(&scenario, cap);
        ts::end(scenario);
    }

    #[test]
    fun devices_need_their_active_key() {
        let mut scenario = setup();
        let device_id = activated_device(&mut scenario, DEVICE);
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
        let owner = ts::take_from_sender<DeviceOwnerCap>(&scenario);
        let id = identity(&policy, NONCE);
        assert!(!seal_policy::device_allowed(&policy, &id, &registry, device_id, DEVICE), 0);
        seal_policy::grant_device(&mut policy, &cap, &registry, device_id);
        assert!(seal_policy::device_allowed(&policy, &id, &registry, device_id, DEVICE), 1);
        // Another address naming the device is not the device.
        assert!(!seal_policy::device_allowed(&policy, &id, &registry, device_id, STRANGER), 2);
        // Registry revocation stops approvals; reinstatement restores them.
        device_registry::revoke_device(&mut registry, &owner);
        assert!(!seal_policy::device_allowed(&policy, &id, &registry, device_id, DEVICE), 3);
        device_registry::reinstate_device(&mut registry, &owner);
        assert!(seal_policy::device_allowed(&policy, &id, &registry, device_id, DEVICE), 4);
        // A rotated identity: the old key is refused, the new key approved.
        device_registry::rotate_device_key(&mut registry, &owner, DEVICE_NEW_KEY);
        assert!(!seal_policy::device_allowed(&policy, &id, &registry, device_id, DEVICE), 5);
        assert!(seal_policy::device_allowed(&policy, &id, &registry, device_id, DEVICE_NEW_KEY), 6);
        // Policy revocation.
        seal_policy::revoke_device(&mut policy, &cap, device_id);
        assert!(!seal_policy::device_allowed(&policy, &id, &registry, device_id, DEVICE_NEW_KEY), 7);
        // An account grant for the device's address is a separate, explicit scope.
        assert!(!seal_policy::account_allowed(&policy, &id, DEVICE_NEW_KEY), 8);
        ts::return_to_sender(&scenario, owner);
        ts::return_shared(registry);
        ts::return_shared(policy);
        ts::return_to_sender(&scenario, cap);
        ts::end(scenario);
    }

    #[test]
    fun deregistered_devices_are_refused() {
        let mut scenario = setup();
        let device_id = activated_device(&mut scenario, DEVICE);
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
        let owner = ts::take_from_sender<DeviceOwnerCap>(&scenario);
        seal_policy::grant_device(&mut policy, &cap, &registry, device_id);
        let id = identity(&policy, NONCE);
        device_registry::deregister_activated_device(&mut registry, owner, ts::ctx(&mut scenario));
        assert!(!seal_policy::device_allowed(&policy, &id, &registry, device_id, DEVICE), 0);
        ts::return_shared(registry);
        ts::return_shared(policy);
        ts::return_to_sender(&scenario, cap);
        ts::end(scenario);
    }

    #[test]
    fun group_grants_follow_current_membership() {
        let mut scenario = setup();
        let device_id = activated_device(&mut scenario, DEVICE);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        device_group::create_group(string::utf8(b"fleet"), string::utf8(b""), &clock, ts::ctx(&mut scenario));
        clock::destroy_for_testing(clock);
        ts::next_tx(&mut scenario, ADMIN);
        let mut group = ts::take_shared<DeviceGroup>(&scenario);
        let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
        let owner = ts::take_from_sender<DeviceOwnerCap>(&scenario);
        let (device, loan) = device_registry::borrow_device(&mut registry, &owner);
        device_group::add_device(&mut group, &device, ts::ctx(&mut scenario));
        device_registry::return_device(&mut registry, device, loan);
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        let id = identity(&policy, NONCE);
        assert!(!seal_policy::group_device_allowed(&policy, &id, &registry, &group, device_id, DEVICE), 0);
        seal_policy::grant_group(&mut policy, &cap, &group, ts::ctx(&mut scenario));
        assert!(seal_policy::group_device_allowed(&policy, &id, &registry, &group, device_id, DEVICE), 1);
        assert!(!seal_policy::group_device_allowed(&policy, &id, &registry, &group, device_id, STRANGER), 2);
        // The group grant does not make the device a direct grantee.
        assert!(!seal_policy::device_allowed(&policy, &id, &registry, device_id, DEVICE), 3);
        device_registry::revoke_device(&mut registry, &owner);
        assert!(!seal_policy::group_device_allowed(&policy, &id, &registry, &group, device_id, DEVICE), 4);
        device_registry::reinstate_device(&mut registry, &owner);
        device_group::remove_device(&mut group, device_id, ts::ctx(&mut scenario));
        assert!(!seal_policy::group_device_allowed(&policy, &id, &registry, &group, device_id, DEVICE), 5);
        seal_policy::revoke_group(&mut policy, &cap, device_group::group_id(&group));
        assert!(seal_policy::groups(&policy).is_empty(), 6);
        ts::return_shared(policy);
        ts::return_to_sender(&scenario, cap);
        ts::return_to_sender(&scenario, owner);
        ts::return_shared(registry);
        ts::return_shared(group);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = seal_policy::ENotGroupAdmin)]
    fun only_the_groups_admin_grants_it() {
        let mut scenario = setup();
        ts::next_tx(&mut scenario, STRANGER);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        device_group::create_group(string::utf8(b"theirs"), string::utf8(b""), &clock, ts::ctx(&mut scenario));
        clock::destroy_for_testing(clock);
        ts::next_tx(&mut scenario, ADMIN);
        let group = ts::take_shared<DeviceGroup>(&scenario);
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        seal_policy::grant_group(&mut policy, &cap, &group, ts::ctx(&mut scenario));
        abort 99
    }

    #[test, expected_failure(abort_code = seal_policy::EUnknownDevice)]
    fun unactivated_devices_cannot_be_granted() {
        let mut scenario = setup();
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        seal_policy::grant_device(&mut policy, &cap, &registry, object::id_from_address(@0x1234));
        abort 99
    }

    #[test, expected_failure(abort_code = seal_policy::EWrongCap)]
    fun another_policys_cap_is_refused() {
        let mut scenario = setup();
        let ours = ts::take_shared<AccessPolicy>(&scenario);
        let ours_id = object::id(&ours);
        ts::return_shared(ours);
        // A stranger's own policy and cap administer only that policy.
        ts::next_tx(&mut scenario, STRANGER);
        let theirs = seal_policy::create_policy(string::utf8(b"theirs"), ts::ctx(&mut scenario));
        ts::next_tx(&mut scenario, STRANGER);
        let mut ours = ts::take_shared_by_id<AccessPolicy>(&scenario, ours_id);
        seal_policy::grant_account(&mut ours, &theirs, STRANGER);
        abort 99
    }

    #[test, expected_failure(abort_code = seal_policy::EAlreadyGranted)]
    fun duplicate_grants_are_refused() {
        let mut scenario = setup();
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        seal_policy::grant_account(&mut policy, &cap, READER);
        seal_policy::grant_account(&mut policy, &cap, READER);
        abort 99
    }

    #[test, expected_failure(abort_code = seal_policy::ENotGranted)]
    fun revoking_an_absent_grant_aborts() {
        let mut scenario = setup();
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        seal_policy::revoke_account(&mut policy, &cap, READER);
        abort 99
    }

    #[test, expected_failure(abort_code = seal_policy::ENoAccess)]
    fun the_entry_approval_aborts_without_access() {
        let mut scenario = setup();
        let device_id = activated_device(&mut scenario, DEVICE);
        let policy = ts::take_shared<AccessPolicy>(&scenario);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        ts::next_tx(&mut scenario, DEVICE);
        seal_policy::seal_approve_device_for_testing(identity(&policy, NONCE), &policy, &registry, device_id, ts::ctx(&mut scenario));
        abort 99
    }

    #[test]
    fun the_entry_approval_passes_for_the_granted_device() {
        let mut scenario = setup();
        let device_id = activated_device(&mut scenario, DEVICE);
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        seal_policy::grant_device(&mut policy, &cap, &registry, device_id);
        ts::next_tx(&mut scenario, DEVICE);
        seal_policy::seal_approve_device_for_testing(identity(&policy, NONCE), &policy, &registry, device_id, ts::ctx(&mut scenario));
        ts::return_shared(registry);
        ts::return_shared(policy);
        ts::return_to_address(ADMIN, cap);
        ts::end(scenario);
    }

    /// Old-code bypass: once a policy moves to a newer code version, this
    /// (older) code refuses every approval and administration on it.
    #[test, expected_failure(abort_code = seal_policy::EWrongVersion)]
    fun older_code_refuses_a_migrated_policy() {
        let mut scenario = setup();
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        seal_policy::grant_account(&mut policy, &cap, READER);
        let id = identity(&policy, NONCE);
        seal_policy::set_version_for_testing(&mut policy, seal_policy::code_version() + 1);
        seal_policy::account_allowed(&policy, &id, READER);
        abort 99
    }

    #[test, expected_failure(abort_code = seal_policy::EWrongVersion)]
    fun older_code_cannot_administer_a_migrated_policy() {
        let mut scenario = setup();
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        seal_policy::set_version_for_testing(&mut policy, seal_policy::code_version() + 1);
        seal_policy::grant_account(&mut policy, &cap, READER);
        abort 99
    }

    #[test]
    fun migration_moves_an_older_policy_forward_once() {
        let mut scenario = setup();
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        seal_policy::set_version_for_testing(&mut policy, seal_policy::code_version() - 1);
        seal_policy::migrate(&mut policy, &cap);
        assert!(seal_policy::version(&policy) == seal_policy::code_version(), 0);
        ts::return_shared(policy);
        ts::return_to_sender(&scenario, cap);
        ts::end(scenario);
    }

    #[test, expected_failure(abort_code = seal_policy::ENotNewer)]
    fun migration_never_repeats_or_downgrades() {
        let mut scenario = setup();
        let mut policy = ts::take_shared<AccessPolicy>(&scenario);
        let cap = ts::take_from_sender<AccessAdminCap>(&scenario);
        seal_policy::migrate(&mut policy, &cap);
        abort 99
    }
}
