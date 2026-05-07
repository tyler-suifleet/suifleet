#[test_only]
module sui_edge::deployment_manager_tests {
    use std::string;
    use sui::clock;
    use sui::test_scenario::{Self as ts};
    use sui_edge::device_registry::{Self, DeviceRegistry};
    use sui_edge::device_cap;
    use sui_edge::device_group;
    use sui_edge::deployment_manager::{Self, DeploymentRecord};

    const ADMIN: address = @0xAD;
    const DEVICE_ADDR: address = @0xDE;
    const OTHER_DEVICE_ADDR: address = @0xFF;

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

        // Add device to the shared group
        let mut group = ts::take_shared<device_group::DeviceGroup>(scenario);
        let group_id = device_group::group_id(&group);

        // Take the device to pass as &Device
        let device = ts::take_from_address<sui_edge::device_registry::Device>(scenario, ADMIN);
        device_group::add_device(&mut group, &device, ts::ctx(scenario));
        ts::return_to_address(ADMIN, device);
        ts::return_shared(group);

        group_id
    }

    fun create_test_deployment(scenario: &mut ts::Scenario, target_groups: vector<ID>) {
        let clock = clock::create_for_testing(ts::ctx(scenario));
        deployment_manager::create_deployment(
            string::utf8(b"my-firmware"),
            string::utf8(b"1.0.0"),
            string::utf8(b"abc123blobid"),
            string::utf8(b"deadbeef...sha256"),
            target_groups,
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
        create_test_deployment(&mut scenario, vector[group_id]);

        // Device reports applied
        ts::next_tx(&mut scenario, DEVICE_ADDR);
        {
            let mut record = ts::take_shared<DeploymentRecord>(&scenario);
            let cap = ts::take_from_sender<sui_edge::device_cap::DeviceCap>(&scenario);

            deployment_manager::report_status(
                &mut record,
                &cap,
                deployment_manager::status_applied(),
                ts::ctx(&mut scenario),
            );

            ts::return_to_sender(&scenario, cap);
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
        create_test_deployment(&mut scenario, vector[group_id]);

        ts::next_tx(&mut scenario, DEVICE_ADDR);
        {
            let mut record = ts::take_shared<DeploymentRecord>(&scenario);
            let cap = ts::take_from_sender<sui_edge::device_cap::DeviceCap>(&scenario);

            deployment_manager::report_status(
                &mut record,
                &cap,
                deployment_manager::status_failed(),
                ts::ctx(&mut scenario),
            );

            ts::return_to_sender(&scenario, cap);
            ts::return_shared(record);
        };

        ts::end(scenario);
    }

    #[test]
    fun test_any_capped_device_can_report() {
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
        create_test_deployment(&mut scenario, vector[group_id]);

        // The other device (not in group) can still report — targeting enforced off-chain.
        // This test verifies report_status accepts any valid DeviceCap.
        ts::next_tx(&mut scenario, OTHER_DEVICE_ADDR);
        {
            let mut record = ts::take_shared<DeploymentRecord>(&scenario);
            let cap = ts::take_from_sender<sui_edge::device_cap::DeviceCap>(&scenario);

            deployment_manager::report_status(
                &mut record,
                &cap,
                deployment_manager::status_applied(),
                ts::ctx(&mut scenario),
            );

            ts::return_to_sender(&scenario, cap);
            ts::return_shared(record);
        };

        ts::end(scenario);
    }
}
