#[test_only]
module sui_edge::deployment_manager_tests {
    use std::string;
    use sui::clock;
    use sui::test_scenario::{Self as ts};
    use sui_edge::device_registry::{Self, DeviceRegistry, DeviceOwnerCap};
    use sui_edge::device_cap::{Self, DeviceCap};
    use sui_edge::device_group;
    use sui_edge::deployment_manager::{Self, DeploymentRecord};

    const ADMIN: address = @0xAD;
    const DEVICE_ADDR: address = @0xDE;
    const OTHER_DEVICE_ADDR: address = @0xFF;

    const NEW_DEVICE_ADDR: address = @0xD2;
    const NEW_OWNER: address = @0xB0;

    fun report(scenario: &mut ts::Scenario, record: &mut DeploymentRecord, cap: &DeviceCap, status: u8) {
        let registry = ts::take_shared<DeviceRegistry>(scenario);
        deployment_manager::report_verified_status(record, &registry, cap, status, ts::ctx(scenario));
        ts::return_shared(registry);
    }

    fun report_for_group(
        scenario: &mut ts::Scenario,
        record: &mut DeploymentRecord,
        cap: &DeviceCap,
        group: &device_group::DeviceGroup,
        status: u8,
    ) {
        let registry = ts::take_shared<DeviceRegistry>(scenario);
        deployment_manager::report_verified_status_for_group(record, &registry, cap, group, status, ts::ctx(scenario));
        ts::return_shared(registry);
    }

    fun setup_registry(scenario: &mut ts::Scenario) {
        ts::next_tx(scenario, ADMIN);
        device_registry::init_for_testing(ts::ctx(scenario));
    }

    fun register_device_with_cap(
        scenario: &mut ts::Scenario,
        name: vector<u8>,
        cap_recipient: address,
    ): ID {
        let mut registry = ts::take_shared<DeviceRegistry>(scenario);
        let clock = clock::create_for_testing(ts::ctx(scenario));

        let device = device_registry::register_device(
            &mut registry,
            string::utf8(name),
            cap_recipient,
            string::utf8(b"aarch64"),
            &clock,
            ts::ctx(scenario),
        );
        let id = device_registry::device_id(&device);
        let cap = device_cap::issue(&device, ts::ctx(scenario));
        transfer::public_transfer(cap, cap_recipient);
        let owner = device_registry::activate_device(&mut registry, device, ts::ctx(scenario));
        transfer::public_transfer(owner, ADMIN);

        clock::destroy_for_testing(clock);
        ts::return_shared(registry);
        id
    }

    /// The owner cap `owner` holds for `device_id`.
    fun owner_cap(scenario: &ts::Scenario, owner: address, device_id: ID): DeviceOwnerCap {
        let ids = ts::ids_for_address<DeviceOwnerCap>(owner);
        let mut i = 0;
        while (i < ids.length()) {
            let cap = ts::take_from_address_by_id<DeviceOwnerCap>(scenario, owner, ids[i]);
            if (device_registry::owner_cap_device_id(&cap) == device_id) return cap;
            ts::return_to_address(owner, cap);
            i = i + 1;
        };
        abort 999
    }

    fun create_group_with_device(scenario: &mut ts::Scenario, device_id: ID): ID {
        let clock = clock::create_for_testing(ts::ctx(scenario));
        device_group::create_group(
            string::utf8(b"test-group"),
            string::utf8(b""),
            &clock,
            ts::ctx(scenario),
        );
        clock::destroy_for_testing(clock);

        ts::next_tx(scenario, ADMIN);

        // Add device to the shared group
        let mut group = ts::take_shared<device_group::DeviceGroup>(scenario);
        let group_id = device_group::group_id(&group);

        // Borrow the custodied device to pass as &Device
        let mut registry = ts::take_shared<DeviceRegistry>(scenario);
        let owner = owner_cap(scenario, ADMIN, device_id);
        let (device, loan) = device_registry::borrow_device(&mut registry, &owner);
        device_group::add_device(&mut group, &device, ts::ctx(scenario));
        device_registry::return_device(&mut registry, device, loan);
        ts::return_to_address(ADMIN, owner);
        ts::return_shared(registry);
        ts::return_shared(group);

        group_id
    }

    fun create_test_deployment(scenario: &mut ts::Scenario, target_groups: vector<ID>, target_devices: vector<ID>) {
        let clock = clock::create_for_testing(ts::ctx(scenario));
        deployment_manager::create_deployment(
            string::utf8(b"my-firmware"),
            string::utf8(b"1.0.0"),
            string::utf8(b"abc123blobid"),
            string::utf8(b"deadbeef...sha256"),
            target_groups,
            target_devices,
            &clock,
            ts::ctx(scenario),
        );
        clock::destroy_for_testing(clock);
    }

    #[test]
    fun test_create_deployment_and_report_applied() {
        let mut scenario = ts::begin(ADMIN);
        setup_registry(&mut scenario);

        ts::next_tx(&mut scenario, ADMIN);
        let _device_id = register_device_with_cap(&mut scenario, b"rpi-001", DEVICE_ADDR);

        ts::next_tx(&mut scenario, ADMIN);
        let group_id = create_group_with_device(&mut scenario, _device_id);

        ts::next_tx(&mut scenario, ADMIN);
        create_test_deployment(&mut scenario, vector[group_id], vector[]);

        // Device reports applied
        ts::next_tx(&mut scenario, DEVICE_ADDR);
        {
            let mut record = ts::take_shared<DeploymentRecord>(&scenario);
            let cap = ts::take_from_sender<sui_edge::device_cap::DeviceCap>(&scenario);

            let group = ts::take_shared<device_group::DeviceGroup>(&scenario);
            report_for_group(&mut scenario, &mut record, &cap, &group, deployment_manager::status_applied());

            ts::return_to_sender(&scenario, cap);
            ts::return_shared(group);
            ts::return_shared(record);
        };

        ts::end(scenario);
    }

    #[test]
    fun test_report_failed() {
        let mut scenario = ts::begin(ADMIN);
        setup_registry(&mut scenario);

        ts::next_tx(&mut scenario, ADMIN);
        let _device_id = register_device_with_cap(&mut scenario, b"rpi-002", DEVICE_ADDR);

        ts::next_tx(&mut scenario, ADMIN);
        let group_id = create_group_with_device(&mut scenario, _device_id);

        ts::next_tx(&mut scenario, ADMIN);
        create_test_deployment(&mut scenario, vector[group_id], vector[]);

        ts::next_tx(&mut scenario, DEVICE_ADDR);
        {
            let mut record = ts::take_shared<DeploymentRecord>(&scenario);
            let cap = ts::take_from_sender<sui_edge::device_cap::DeviceCap>(&scenario);

            let group = ts::take_shared<device_group::DeviceGroup>(&scenario);
            report_for_group(&mut scenario, &mut record, &cap, &group, deployment_manager::status_failed());

            ts::return_to_sender(&scenario, cap);
            ts::return_shared(group);
            ts::return_shared(record);
        };

        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 3, location = sui_edge::deployment_manager)]
    fun test_non_member_cannot_report() {
        let mut scenario = ts::begin(ADMIN);
        setup_registry(&mut scenario);

        // Register two devices, only put one in the group
        ts::next_tx(&mut scenario, ADMIN);
        let _device_id = register_device_with_cap(&mut scenario, b"targeted", DEVICE_ADDR);

        ts::next_tx(&mut scenario, ADMIN);
        {
            let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
            let clock = clock::create_for_testing(ts::ctx(&mut scenario));
            let other = device_registry::register_device(
                &mut registry,
                string::utf8(b"other"),
                OTHER_DEVICE_ADDR,
                string::utf8(b"aarch64"),
                &clock,
                ts::ctx(&mut scenario),
            );
            let other_cap = device_cap::issue(&other, ts::ctx(&mut scenario));
            transfer::public_transfer(other_cap, OTHER_DEVICE_ADDR);
            let owner = device_registry::activate_device(&mut registry, other, ts::ctx(&mut scenario));
            transfer::public_transfer(owner, ADMIN);
            clock::destroy_for_testing(clock);
            ts::return_shared(registry);
        };

        ts::next_tx(&mut scenario, ADMIN);
        let group_id = create_group_with_device(&mut scenario, _device_id);

        ts::next_tx(&mut scenario, ADMIN);
        create_test_deployment(&mut scenario, vector[group_id], vector[]);

        // A valid cap does not grant status authority for an unrelated group.
        ts::next_tx(&mut scenario, OTHER_DEVICE_ADDR);
        {
            let mut record = ts::take_shared<DeploymentRecord>(&scenario);
            let cap = ts::take_from_sender<sui_edge::device_cap::DeviceCap>(&scenario);

            let group = ts::take_shared<device_group::DeviceGroup>(&scenario);
            report_for_group(&mut scenario, &mut record, &cap, &group, deployment_manager::status_applied());

            ts::return_to_sender(&scenario, cap);
            ts::return_shared(group);
            ts::return_shared(record);
        };

        ts::end(scenario);
    }
    #[test]
    fun test_direct_target_reports_applied() {
        let mut scenario = ts::begin(ADMIN);
        setup_registry(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let id = register_device_with_cap(&mut scenario, b"direct", DEVICE_ADDR);
        ts::next_tx(&mut scenario, ADMIN);
        create_test_deployment(&mut scenario, vector[], vector[id]);
        ts::next_tx(&mut scenario, DEVICE_ADDR);
        let mut record = ts::take_shared<DeploymentRecord>(&scenario);
        let cap = ts::take_from_sender<sui_edge::device_cap::DeviceCap>(&scenario);
        report(&mut scenario, &mut record, &cap, 1);
        assert!(deployment_manager::receipt_status(&record, id) == option::some(1), 100);
        ts::return_to_sender(&scenario, cap);
        ts::return_shared(record);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 3, location = sui_edge::deployment_manager)]
    fun test_unrelated_direct_target_rejected() {
        let mut scenario = ts::begin(ADMIN);
        setup_registry(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let _id = register_device_with_cap(&mut scenario, b"other", DEVICE_ADDR);
        ts::next_tx(&mut scenario, ADMIN);
        create_test_deployment(&mut scenario, vector[], vector[object::id_from_address(@0x123)]);
        ts::next_tx(&mut scenario, DEVICE_ADDR);
        let mut record = ts::take_shared<DeploymentRecord>(&scenario);
        let cap = ts::take_from_sender<sui_edge::device_cap::DeviceCap>(&scenario);
        report(&mut scenario, &mut record, &cap, 1);
        ts::return_to_sender(&scenario, cap);
        ts::return_shared(record);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 0, location = sui_edge::deployment_manager)]
    fun test_invalid_status_rejected() {
        let mut scenario = ts::begin(ADMIN);
        setup_registry(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let id = register_device_with_cap(&mut scenario, b"invalid-status", DEVICE_ADDR);
        ts::next_tx(&mut scenario, ADMIN);
        create_test_deployment(&mut scenario, vector[], vector[id]);
        ts::next_tx(&mut scenario, DEVICE_ADDR);
        let mut record = ts::take_shared<DeploymentRecord>(&scenario);
        let cap = ts::take_from_sender<sui_edge::device_cap::DeviceCap>(&scenario);
        report(&mut scenario, &mut record, &cap, 7);
        ts::return_to_sender(&scenario, cap);
        ts::return_shared(record);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 1, location = sui_edge::deployment_manager)]
    fun test_empty_targets_rejected() {
        let mut scenario = ts::begin(ADMIN);
        create_test_deployment(&mut scenario, vector[], vector[]);
        ts::end(scenario);
    }

    #[test]
    fun test_verified_recipe_digest_and_cleanup() {
        let mut scenario = ts::begin(ADMIN);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        let digest = string::utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
        deployment_manager::create_deployment_verified(
            string::utf8(b"app"), string::utf8(b"1.0.0"), string::utf8(b"recipe"),
            string::utf8(b"artifact-hash"), digest, vector[], vector[object::id_from_address(DEVICE_ADDR)],
            &clock, ts::ctx(&mut scenario));
        clock::destroy_for_testing(clock);
        ts::next_tx(&mut scenario, ADMIN);
        let record = ts::take_shared<DeploymentRecord>(&scenario);
        assert!(*deployment_manager::recipe_sha256(&record) == digest, 100);
        deployment_manager::delete_deployment(record, ts::ctx(&mut scenario));
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 4, location = sui_edge::deployment_manager)]
    fun test_invalid_recipe_digest_rejected() {
        let mut scenario = ts::begin(ADMIN);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        deployment_manager::create_deployment_verified(
            string::utf8(b"app"), string::utf8(b"1.0.0"), string::utf8(b"recipe"),
            string::utf8(b"artifact-hash"), string::utf8(b"gggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggg"),
            vector[], vector[object::id_from_address(DEVICE_ADDR)], &clock, ts::ctx(&mut scenario));
        clock::destroy_for_testing(clock);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 0, location = sui_edge::device_cap)]
    fun test_operator_cannot_impersonate_device() {
        let mut scenario = ts::begin(ADMIN);
        setup_registry(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        let device = device_registry::register_device(&mut registry, string::utf8(b"bound"),
            DEVICE_ADDR, string::utf8(b"aarch64"), &clock, ts::ctx(&mut scenario));
        let id = device_registry::device_id(&device);
        let cap = device_cap::issue(&device, ts::ctx(&mut scenario));
        // Even a public capability deliberately transferred to the operator must
        // not allow the operator to forge an applied report for the device.
        transfer::public_transfer(cap, ADMIN);
        transfer::public_transfer(device, ADMIN);
        ts::return_shared(registry);
        clock::destroy_for_testing(clock);
        ts::next_tx(&mut scenario, ADMIN);
        create_test_deployment(&mut scenario, vector[], vector[id]);
        ts::next_tx(&mut scenario, ADMIN);
        let cap = ts::take_from_sender<sui_edge::device_cap::DeviceCap>(&scenario);
        let mut record = ts::take_shared<DeploymentRecord>(&scenario);
        report(&mut scenario, &mut record, &cap, 1);
        ts::return_to_sender(&scenario, cap);
        ts::return_shared(record);
        ts::end(scenario);
    }

    // ── Upgrade security: old package entry points ───────────────────────────
    //
    // Unit tests cannot load an older package version; the `*_for_testing`
    // helpers replay the original package's unchecked report body. The real
    // old/new package check is scripts/upgrade_security_localnet.sh.

    #[test]
    fun test_new_record_has_receipts_and_no_legacy_statuses() {
        let mut scenario = ts::begin(ADMIN);
        setup_registry(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let id = register_device_with_cap(&mut scenario, b"fresh", DEVICE_ADDR);
        ts::next_tx(&mut scenario, ADMIN);
        create_test_deployment(&mut scenario, vector[], vector[id]);
        ts::next_tx(&mut scenario, DEVICE_ADDR);
        let record = ts::take_shared<DeploymentRecord>(&scenario);
        assert!(deployment_manager::has_receipts(&record), 100);
        assert!(deployment_manager::receipts_version(&record) == 2, 101);
        assert!(deployment_manager::receipt_status(&record, id).is_none(), 102);
        ts::return_shared(record);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = sui::dynamic_field::EFieldDoesNotExist)]
    fun test_old_report_path_cannot_write_new_record() {
        let mut scenario = ts::begin(ADMIN);
        setup_registry(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let id = register_device_with_cap(&mut scenario, b"fresh", DEVICE_ADDR);
        ts::next_tx(&mut scenario, ADMIN);
        create_test_deployment(&mut scenario, vector[], vector[id]);
        // Anyone holding any cap could call the original report_status.
        ts::next_tx(&mut scenario, OTHER_DEVICE_ADDR);
        let mut record = ts::take_shared<DeploymentRecord>(&scenario);
        deployment_manager::legacy_report_for_testing(&mut record, id, 1);
        ts::return_shared(record);
        ts::end(scenario);
    }

    #[test]
    fun test_legacy_forgery_does_not_reach_receipts() {
        let mut scenario = ts::begin(ADMIN);
        setup_registry(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let id = register_device_with_cap(&mut scenario, b"legacy", DEVICE_ADDR);
        ts::next_tx(&mut scenario, ADMIN);
        deployment_manager::create_legacy_for_testing(vector[id], ts::ctx(&mut scenario));

        // Forged "applied" through the old entry point is kept as history only.
        ts::next_tx(&mut scenario, OTHER_DEVICE_ADDR);
        {
            let mut record = ts::take_shared<DeploymentRecord>(&scenario);
            deployment_manager::legacy_report_for_testing(&mut record, id, 1);
            assert!(!deployment_manager::has_receipts(&record), 100);
            assert!(deployment_manager::receipt_status(&record, id).is_none(), 101);
            ts::return_shared(record);
        };

        // The real device reports failure; receipts reflect only that report.
        ts::next_tx(&mut scenario, DEVICE_ADDR);
        {
            let mut record = ts::take_shared<DeploymentRecord>(&scenario);
            let cap = ts::take_from_sender<sui_edge::device_cap::DeviceCap>(&scenario);
            report(&mut scenario, &mut record, &cap, 2);
            assert!(deployment_manager::receipt_status(&record, id) == option::some(2), 102);
            assert!(*deployment_manager::statuses(&record).get(&id) == 1, 103);
            ts::return_to_sender(&scenario, cap);
            ts::return_shared(record);
        };

        // A later forgery still cannot change the receipt.
        ts::next_tx(&mut scenario, OTHER_DEVICE_ADDR);
        {
            let mut record = ts::take_shared<DeploymentRecord>(&scenario);
            deployment_manager::legacy_report_for_testing(&mut record, id, 1);
            assert!(deployment_manager::receipt_status(&record, id) == option::some(2), 104);
            ts::return_shared(record);
        };
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 5, location = sui_edge::deployment_manager)]
    fun test_report_aborts_after_newer_package_migration() {
        let mut scenario = ts::begin(ADMIN);
        setup_registry(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let id = register_device_with_cap(&mut scenario, b"gated", DEVICE_ADDR);
        ts::next_tx(&mut scenario, ADMIN);
        create_test_deployment(&mut scenario, vector[], vector[id]);
        ts::next_tx(&mut scenario, DEVICE_ADDR);
        let mut record = ts::take_shared<DeploymentRecord>(&scenario);
        let cap = ts::take_from_sender<sui_edge::device_cap::DeviceCap>(&scenario);
        deployment_manager::set_receipts_version_for_testing(&mut record, 3);
        report(&mut scenario, &mut record, &cap, 1);
        ts::return_to_sender(&scenario, cap);
        ts::return_shared(record);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 5, location = sui_edge::deployment_manager)]
    fun test_migrate_never_downgrades() {
        let mut scenario = ts::begin(ADMIN);
        setup_registry(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let id = register_device_with_cap(&mut scenario, b"gated", DEVICE_ADDR);
        ts::next_tx(&mut scenario, ADMIN);
        create_test_deployment(&mut scenario, vector[], vector[id]);
        ts::next_tx(&mut scenario, OTHER_DEVICE_ADDR);
        let mut record = ts::take_shared<DeploymentRecord>(&scenario);
        deployment_manager::set_receipts_version_for_testing(&mut record, 3);
        deployment_manager::migrate(&mut record);
        ts::return_shared(record);
        ts::end(scenario);
    }

    #[test]
    fun test_migrate_attaches_receipts_to_legacy_record_and_delete_cleans_up() {
        let mut scenario = ts::begin(ADMIN);
        setup_registry(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let id = register_device_with_cap(&mut scenario, b"legacy", DEVICE_ADDR);
        ts::next_tx(&mut scenario, ADMIN);
        deployment_manager::create_legacy_for_testing(vector[id], ts::ctx(&mut scenario));
        ts::next_tx(&mut scenario, OTHER_DEVICE_ADDR);
        {
            let mut record = ts::take_shared<DeploymentRecord>(&scenario);
            deployment_manager::legacy_report_for_testing(&mut record, id, 1);
            deployment_manager::migrate(&mut record);
            deployment_manager::migrate(&mut record);
            assert!(deployment_manager::receipts_version(&record) == 2, 100);
            assert!(deployment_manager::receipt_statuses(&record).is_empty(), 101);
            ts::return_shared(record);
        };
        ts::next_tx(&mut scenario, ADMIN);
        let record = ts::take_shared<DeploymentRecord>(&scenario);
        deployment_manager::delete_deployment(record, ts::ctx(&mut scenario));
        ts::end(scenario);
    }

    // ── Identity lifecycle (revocation, rotation, deregistration) ────────────

    /// Device `id` with a cap at DEVICE_ADDR and a direct deployment for it.
    fun device_with_deployment(scenario: &mut ts::Scenario): ID {
        setup_registry(scenario);
        ts::next_tx(scenario, ADMIN);
        let id = register_device_with_cap(scenario, b"lifecycle", DEVICE_ADDR);
        ts::next_tx(scenario, ADMIN);
        create_test_deployment(scenario, vector[], vector[id]);
        id
    }

    fun report_as(scenario: &mut ts::Scenario, sender: address, status: u8) {
        ts::next_tx(scenario, sender);
        let mut record = ts::take_shared<DeploymentRecord>(scenario);
        let cap = ts::take_from_sender<DeviceCap>(scenario);
        report(scenario, &mut record, &cap, status);
        ts::return_to_sender(scenario, cap);
        ts::return_shared(record);
    }

    fun revoke(scenario: &mut ts::Scenario, owner: address, id: ID) {
        ts::next_tx(scenario, owner);
        let mut registry = ts::take_shared<DeviceRegistry>(scenario);
        let cap = owner_cap(scenario, owner, id);
        device_registry::revoke_device(&mut registry, &cap);
        ts::return_to_address(owner, cap);
        ts::return_shared(registry);
    }

    fun reinstate(scenario: &mut ts::Scenario, owner: address, id: ID) {
        ts::next_tx(scenario, owner);
        let mut registry = ts::take_shared<DeviceRegistry>(scenario);
        let cap = owner_cap(scenario, owner, id);
        device_registry::reinstate_device(&mut registry, &cap);
        ts::return_to_address(owner, cap);
        ts::return_shared(registry);
    }

    /// A device registered but never activated, as every device registered
    /// before identities existed is: owned directly, with a signer-bound cap.
    fun unactivated_device_with_deployment(scenario: &mut ts::Scenario): ID {
        setup_registry(scenario);
        ts::next_tx(scenario, ADMIN);
        let mut registry = ts::take_shared<DeviceRegistry>(scenario);
        let clock = clock::create_for_testing(ts::ctx(scenario));
        let device = device_registry::register_device(&mut registry, string::utf8(b"legacy"),
            DEVICE_ADDR, string::utf8(b"aarch64"), &clock, ts::ctx(scenario));
        let id = device_registry::device_id(&device);
        transfer::public_transfer(device_cap::issue(&device, ts::ctx(scenario)), DEVICE_ADDR);
        transfer::public_transfer(device, ADMIN);
        clock::destroy_for_testing(clock);
        ts::return_shared(registry);
        ts::next_tx(scenario, ADMIN);
        create_test_deployment(scenario, vector[], vector[id]);
        id
    }

    #[test]
    #[expected_failure(abort_code = 7, location = sui_edge::deployment_manager)]
    fun test_unverified_report_signature_is_retired() {
        let mut scenario = ts::begin(ADMIN);
        let _id = device_with_deployment(&mut scenario);
        ts::next_tx(&mut scenario, DEVICE_ADDR);
        let mut record = ts::take_shared<DeploymentRecord>(&scenario);
        let cap = ts::take_from_sender<DeviceCap>(&scenario);
        deployment_manager::report_status(&mut record, &cap, 1, ts::ctx(&mut scenario));
        ts::return_to_sender(&scenario, cap);
        ts::return_shared(record);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 2, location = sui_edge::device_cap)]
    fun test_revoked_device_cannot_report() {
        let mut scenario = ts::begin(ADMIN);
        let id = device_with_deployment(&mut scenario);
        revoke(&mut scenario, ADMIN, id);
        report_as(&mut scenario, DEVICE_ADDR, 1);
        ts::end(scenario);
    }

    #[test]
    fun test_reinstatement_after_revocation_restores_reporting() {
        let mut scenario = ts::begin(ADMIN);
        let id = device_with_deployment(&mut scenario);
        revoke(&mut scenario, ADMIN, id);
        reinstate(&mut scenario, ADMIN, id);
        report_as(&mut scenario, DEVICE_ADDR, 1);
        ts::next_tx(&mut scenario, ADMIN);
        let record = ts::take_shared<DeploymentRecord>(&scenario);
        assert!(deployment_manager::receipt_status(&record, id) == option::some(1), 100);
        ts::return_shared(record);
        ts::end(scenario);
    }

    /// Rotating keeps the device but distrusts every cap bound to the old key.
    fun rotate(scenario: &mut ts::Scenario, id: ID) {
        ts::next_tx(scenario, ADMIN);
        let mut registry = ts::take_shared<DeviceRegistry>(scenario);
        let owner = owner_cap(scenario, ADMIN, id);
        device_registry::rotate_device_key(&mut registry, &owner, NEW_DEVICE_ADDR);
        let (device, loan) = device_registry::borrow_device(&mut registry, &owner);
        let cap = device_cap::issue(&device, ts::ctx(scenario));
        device_registry::return_device(&mut registry, device, loan);
        transfer::public_transfer(cap, NEW_DEVICE_ADDR);
        ts::return_to_address(ADMIN, owner);
        ts::return_shared(registry);
    }

    #[test]
    #[expected_failure(abort_code = 2, location = sui_edge::device_cap)]
    fun test_rotated_out_key_cannot_report() {
        let mut scenario = ts::begin(ADMIN);
        let _id = device_with_deployment(&mut scenario);
        rotate(&mut scenario, _id);
        report_as(&mut scenario, DEVICE_ADDR, 1);
        ts::end(scenario);
    }

    #[test]
    fun test_rotated_in_key_reports() {
        let mut scenario = ts::begin(ADMIN);
        let id = device_with_deployment(&mut scenario);
        rotate(&mut scenario, id);
        report_as(&mut scenario, NEW_DEVICE_ADDR, 2);
        ts::next_tx(&mut scenario, ADMIN);
        let record = ts::take_shared<DeploymentRecord>(&scenario);
        assert!(deployment_manager::receipt_status(&record, id) == option::some(2), 100);
        ts::return_shared(record);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 2, location = sui_edge::device_cap)]
    fun test_rotation_does_not_lift_a_revocation() {
        let mut scenario = ts::begin(ADMIN);
        let id = device_with_deployment(&mut scenario);
        revoke(&mut scenario, ADMIN, id);
        rotate(&mut scenario, id);
        report_as(&mut scenario, NEW_DEVICE_ADDR, 1);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 2, location = sui_edge::device_cap)]
    fun test_deregistered_device_cannot_report() {
        let mut scenario = ts::begin(ADMIN);
        let id = device_with_deployment(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        {
            let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
            let owner = owner_cap(&scenario, ADMIN, id);
            device_registry::deregister_activated_device(&mut registry, owner, ts::ctx(&mut scenario));
            let identity = device_registry::identity(&registry, id).destroy_some();
            assert!(device_registry::identity_revoked(&identity), 100);
            assert!(!device_registry::is_custodied(&registry, id), 101);
            ts::return_shared(registry);
        };
        report_as(&mut scenario, DEVICE_ADDR, 1);
        ts::end(scenario);
    }

    #[test]
    fun test_activated_device_cannot_be_taken_by_value() {
        // Older packages' deregister_device needs the Device by value; after
        // activation the owner holds only a DeviceOwnerCap.
        let mut scenario = ts::begin(ADMIN);
        let id = device_with_deployment(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        assert!(ts::ids_for_address<device_registry::Device>(ADMIN).is_empty(), 100);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        assert!(device_registry::is_custodied(&registry, id), 101);
        assert!(device_registry::device_address(device_registry::custodied_device(&registry, id)) == DEVICE_ADDR, 102);
        ts::return_shared(registry);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 2, location = sui_edge::device_cap)]
    fun test_unactivated_device_is_untrusted() {
        let mut scenario = ts::begin(ADMIN);
        let _id = unactivated_device_with_deployment(&mut scenario);
        report_as(&mut scenario, DEVICE_ADDR, 1);
        ts::end(scenario);
    }

    #[test]
    fun test_legacy_device_activation_keeps_its_bound_cap() {
        let mut scenario = ts::begin(ADMIN);
        let id = unactivated_device_with_deployment(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        {
            let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
            let device = ts::take_from_sender_by_id<device_registry::Device>(&scenario, id);
            let owner = device_registry::activate_device(&mut registry, device, ts::ctx(&mut scenario));
            transfer::public_transfer(owner, ADMIN);
            ts::return_shared(registry);
        };
        report_as(&mut scenario, DEVICE_ADDR, 1);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 2, location = sui_edge::device_cap)]
    fun test_ownership_transfer_moves_lifecycle_authority() {
        let mut scenario = ts::begin(ADMIN);
        let id = device_with_deployment(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        transfer::public_transfer(owner_cap(&scenario, ADMIN, id), NEW_OWNER);
        ts::next_tx(&mut scenario, ADMIN);
        assert!(ts::ids_for_address<DeviceOwnerCap>(ADMIN).is_empty(), 100);
        revoke(&mut scenario, NEW_OWNER, id);
        report_as(&mut scenario, DEVICE_ADDR, 1);
        ts::end(scenario);
    }

    // ── Receipts from an older report authority ──────────────────────────────

    /// A record whose receipts are still at the previous authority version, as
    /// one created by (or never migrated from) the previous package is.
    fun previous_version_record(scenario: &mut ts::Scenario): ID {
        let id = device_with_deployment(scenario);
        ts::next_tx(scenario, ADMIN);
        let mut record = ts::take_shared<DeploymentRecord>(scenario);
        deployment_manager::set_receipts_version_for_testing(&mut record, 1);
        ts::return_shared(record);
        id
    }

    #[test]
    fun test_revoked_key_report_through_previous_package_is_not_a_receipt() {
        let mut scenario = ts::begin(ADMIN);
        let id = previous_version_record(&mut scenario);
        revoke(&mut scenario, ADMIN, id);
        // The previous package only checks the cap's signer binding.
        ts::next_tx(&mut scenario, DEVICE_ADDR);
        {
            let mut record = ts::take_shared<DeploymentRecord>(&scenario);
            deployment_manager::old_package_report_for_testing(&mut record, id, 1);
            assert!(deployment_manager::has_receipts(&record), 100);
            assert!(!deployment_manager::has_current_receipts(&record), 101);
            assert!(deployment_manager::receipt_status(&record, id).is_none(), 102);
            // Migration discards it rather than promoting it.
            deployment_manager::migrate(&mut record);
            assert!(deployment_manager::has_current_receipts(&record), 103);
            assert!(deployment_manager::receipt_statuses(&record).is_empty(), 104);
            ts::return_shared(record);
        };
        ts::end(scenario);
    }

    #[test]
    fun test_current_report_replaces_previous_version_statuses() {
        let mut scenario = ts::begin(ADMIN);
        let id = previous_version_record(&mut scenario);
        let other = object::id_from_address(OTHER_DEVICE_ADDR);
        ts::next_tx(&mut scenario, OTHER_DEVICE_ADDR);
        {
            let mut record = ts::take_shared<DeploymentRecord>(&scenario);
            deployment_manager::old_package_report_for_testing(&mut record, other, 1);
            deployment_manager::old_package_report_for_testing(&mut record, id, 1);
            ts::return_shared(record);
        };
        report_as(&mut scenario, DEVICE_ADDR, 2);
        ts::next_tx(&mut scenario, ADMIN);
        let record = ts::take_shared<DeploymentRecord>(&scenario);
        assert!(deployment_manager::receipts_version(&record) == 2, 100);
        assert!(deployment_manager::receipt_status(&record, id) == option::some(2), 101);
        assert!(deployment_manager::receipt_status(&record, other).is_none(), 102);
        assert!(deployment_manager::receipt_statuses(&record).size() == 1, 103);
        ts::return_shared(record);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 8, location = sui_edge::deployment_manager)]
    fun test_previous_version_statuses_are_not_readable_as_receipts() {
        let mut scenario = ts::begin(ADMIN);
        let _id = previous_version_record(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let record = ts::take_shared<DeploymentRecord>(&scenario);
        deployment_manager::receipt_statuses(&record);
        ts::return_shared(record);
        ts::end(scenario);
    }
}
