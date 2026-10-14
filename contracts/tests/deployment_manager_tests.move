#[test_only]
module sui_edge::deployment_manager_tests {
    use std::string;
    use sui::clock;
    use sui::test_scenario::{Self as ts};
    use sui_edge::device_registry::{Self, DeviceRegistry};
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
        transfer::public_transfer(device, ADMIN);

        clock::destroy_for_testing(clock);
        ts::return_shared(registry);
        id
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

        // Take the device to pass as &Device
        let device = ts::take_from_address_by_id<sui_edge::device_registry::Device>(scenario, ADMIN, device_id);
        device_group::add_device(&mut group, &device, ts::ctx(scenario));
        ts::return_to_address(ADMIN, device);
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
            transfer::public_transfer(other, ADMIN);
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

    fun revoke(scenario: &mut ts::Scenario, sender: address, id: ID) {
        ts::next_tx(scenario, sender);
        let mut registry = ts::take_shared<DeviceRegistry>(scenario);
        device_registry::revoke_device(&mut registry, id, ts::ctx(scenario));
        ts::return_shared(registry);
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
    fun test_reactivation_after_revocation_restores_reporting() {
        let mut scenario = ts::begin(ADMIN);
        let id = device_with_deployment(&mut scenario);
        revoke(&mut scenario, ADMIN, id);
        ts::next_tx(&mut scenario, ADMIN);
        {
            let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
            let device = ts::take_from_sender_by_id<device_registry::Device>(&scenario, id);
            device_registry::activate_device(&mut registry, &device, ts::ctx(&mut scenario));
            ts::return_to_sender(&scenario, device);
            ts::return_shared(registry);
        };
        report_as(&mut scenario, DEVICE_ADDR, 1);
        ts::next_tx(&mut scenario, ADMIN);
        let record = ts::take_shared<DeploymentRecord>(&scenario);
        assert!(deployment_manager::receipt_status(&record, id) == option::some(1), 100);
        ts::return_shared(record);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 1, location = sui_edge::device_registry)]
    fun test_only_the_operator_can_revoke() {
        let mut scenario = ts::begin(ADMIN);
        let id = device_with_deployment(&mut scenario);
        revoke(&mut scenario, OTHER_DEVICE_ADDR, id);
        ts::end(scenario);
    }

    /// Rotating keeps the device but distrusts every cap bound to the old key.
    fun rotate(scenario: &mut ts::Scenario, id: ID) {
        ts::next_tx(scenario, ADMIN);
        let mut registry = ts::take_shared<DeviceRegistry>(scenario);
        let mut device = ts::take_from_sender_by_id<device_registry::Device>(scenario, id);
        device_registry::rotate_device_key(&mut registry, &mut device, NEW_DEVICE_ADDR, ts::ctx(scenario));
        let cap = device_cap::issue(&device, ts::ctx(scenario));
        transfer::public_transfer(cap, NEW_DEVICE_ADDR);
        ts::return_to_sender(scenario, device);
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
    fun test_deregistered_device_cannot_report() {
        let mut scenario = ts::begin(ADMIN);
        let id = device_with_deployment(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        {
            let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
            let device = ts::take_from_sender_by_id<device_registry::Device>(&scenario, id);
            device_registry::deregister_device(&mut registry, device, ts::ctx(&mut scenario));
            let identity = device_registry::identity(&registry, id).destroy_some();
            assert!(device_registry::identity_revoked(&identity), 100);
            ts::return_shared(registry);
        };
        report_as(&mut scenario, DEVICE_ADDR, 1);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 2, location = sui_edge::device_cap)]
    fun test_legacy_device_without_identity_is_untrusted() {
        let mut scenario = ts::begin(ADMIN);
        let id = device_with_deployment(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        {
            let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
            device_registry::remove_identity_for_testing(&mut registry, id);
            ts::return_shared(registry);
        };
        report_as(&mut scenario, DEVICE_ADDR, 1);
        ts::end(scenario);
    }

    #[test]
    fun test_legacy_device_activation_keeps_its_bound_cap() {
        let mut scenario = ts::begin(ADMIN);
        let id = device_with_deployment(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        {
            let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
            device_registry::remove_identity_for_testing(&mut registry, id);
            let device = ts::take_from_sender_by_id<device_registry::Device>(&scenario, id);
            device_registry::activate_device(&mut registry, &device, ts::ctx(&mut scenario));
            ts::return_to_sender(&scenario, device);
            ts::return_shared(registry);
        };
        report_as(&mut scenario, DEVICE_ADDR, 1);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 1, location = sui_edge::device_registry)]
    fun test_ownership_transfer_moves_revocation_authority() {
        let mut scenario = ts::begin(ADMIN);
        let id = device_with_deployment(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        {
            let device = ts::take_from_sender_by_id<device_registry::Device>(&scenario, id);
            transfer::public_transfer(device, NEW_OWNER);
        };
        ts::next_tx(&mut scenario, NEW_OWNER);
        {
            let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
            let device = ts::take_from_sender_by_id<device_registry::Device>(&scenario, id);
            device_registry::activate_device(&mut registry, &device, ts::ctx(&mut scenario));
            let identity = device_registry::identity(&registry, id).destroy_some();
            assert!(device_registry::identity_operator(&identity) == NEW_OWNER, 100);
            ts::return_to_sender(&scenario, device);
            ts::return_shared(registry);
        };
        // The previous owner can no longer revoke.
        revoke(&mut scenario, ADMIN, id);
        ts::end(scenario);
    }
}
