#[test_only]
module sui_edge::device_registry_tests {
    use std::string;
    use sui::clock;
    use sui::test_scenario::{Self as ts, Scenario};
    use sui_edge::device_registry::{Self, DeviceRegistry};
    use sui_edge::device_cap;

    const ADMIN: address = @0xAD;
    const DEVICE_ADDR: address = @0xDE;

    fun setup(scenario: &mut Scenario) {
        ts::next_tx(scenario, ADMIN);
        {
            // init is called automatically by publish; simulate it here
            let ctx = ts::ctx(scenario);
            device_registry::init_for_testing(ctx);
        };
    }

    #[test]
    fun test_register_and_deregister() {
        let mut scenario = ts::begin(ADMIN);
        setup(&mut scenario);

        ts::next_tx(&mut scenario, ADMIN);
        {
            let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
            let clock = clock::create_for_testing(ts::ctx(&mut scenario));

            let device = device_registry::register_device(
                &mut registry,
                string::utf8(b"rpi-001"),
                DEVICE_ADDR,
                string::utf8(b"aarch64"),
                &clock,
                ts::ctx(&mut scenario),
            );

            assert!(device_registry::device_count(&registry) == 1, 0);

            device_registry::deregister_device(
                &mut registry,
                device,
                ts::ctx(&mut scenario),
            );

            assert!(device_registry::device_count(&registry) == 0, 1);

            clock::destroy_for_testing(clock);
            ts::return_shared(registry);
        };

        ts::end(scenario);
    }

    #[test]
    fun test_issue_device_cap() {
        let mut scenario = ts::begin(ADMIN);
        setup(&mut scenario);

        ts::next_tx(&mut scenario, ADMIN);
        {
            let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
            let clock = clock::create_for_testing(ts::ctx(&mut scenario));

            let device = device_registry::register_device(
                &mut registry,
                string::utf8(b"rpi-002"),
                DEVICE_ADDR,
                string::utf8(b"aarch64"),
                &clock,
                ts::ctx(&mut scenario),
            );

            let cap = device_cap::issue(&device, ts::ctx(&mut scenario));
            assert!(device_cap::device_id(&cap) == device_registry::device_id(&device), 0);

            transfer::public_transfer(cap, DEVICE_ADDR);
            transfer::public_transfer(device, ADMIN);

            clock::destroy_for_testing(clock);
            ts::return_shared(registry);
        };

        ts::end(scenario);
    }

    #[test]
    fun test_set_meta() {
        let mut scenario = ts::begin(ADMIN);
        setup(&mut scenario);

        ts::next_tx(&mut scenario, ADMIN);
        {
            let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
            let clock = clock::create_for_testing(ts::ctx(&mut scenario));

            let mut device = device_registry::register_device(
                &mut registry,
                string::utf8(b"rpi-003"),
                DEVICE_ADDR,
                string::utf8(b"x86_64"),
                &clock,
                ts::ctx(&mut scenario),
            );

            device_registry::set_meta(
                &mut device,
                string::utf8(b"board"),
                string::utf8(b"raspberry-pi-4"),
                ts::ctx(&mut scenario),
            );

            transfer::public_transfer(device, ADMIN);
            clock::destroy_for_testing(clock);
            ts::return_shared(registry);
        };

        ts::end(scenario);
    }

    fun register(registry: &mut DeviceRegistry, name: vector<u8>, scenario: &mut Scenario): device_registry::Device {
        let clock = clock::create_for_testing(ts::ctx(scenario));
        let device = device_registry::register_device(registry, string::utf8(name), DEVICE_ADDR,
            string::utf8(b"aarch64"), &clock, ts::ctx(scenario));
        clock::destroy_for_testing(clock);
        device
    }

    #[test]
    fun test_activated_device_lifecycle_through_its_owner_cap() {
        let mut scenario = ts::begin(ADMIN);
        setup(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
        let device = register(&mut registry, b"rpi-004", &mut scenario);
        let id = device_registry::device_id(&device);
        assert!(!device_registry::is_active_signer(&registry, id, DEVICE_ADDR), 0);
        let owner = device_registry::activate_device(&mut registry, device, ts::ctx(&mut scenario));
        assert!(device_registry::is_active_signer(&registry, id, DEVICE_ADDR), 1);

        // Borrowed devices serve `&Device` APIs and must come back unchanged.
        let (mut device, loan) = device_registry::borrow_device(&mut registry, &owner);
        assert!(!device_registry::is_active_signer(&registry, id, DEVICE_ADDR), 2);
        device_registry::set_meta(&mut device, string::utf8(b"board"), string::utf8(b"rpi3"), ts::ctx(&mut scenario));
        device_registry::return_device(&mut registry, device, loan);
        assert!(device_registry::is_active_signer(&registry, id, DEVICE_ADDR), 3);

        device_registry::deregister_activated_device(&mut registry, owner, ts::ctx(&mut scenario));
        assert!(device_registry::device_count(&registry) == 0, 4);
        assert!(!device_registry::is_active_signer(&registry, id, DEVICE_ADDR), 5);
        ts::return_shared(registry);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 1, location = sui_edge::device_registry)]
    fun test_loan_cannot_be_repaid_with_another_device() {
        let mut scenario = ts::begin(ADMIN);
        setup(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
        let device = register(&mut registry, b"custodied", &mut scenario);
        let owner = device_registry::activate_device(&mut registry, device, ts::ctx(&mut scenario));
        let (borrowed, loan) = device_registry::borrow_device(&mut registry, &owner);
        let substitute = register(&mut registry, b"substitute", &mut scenario);
        device_registry::return_device(&mut registry, substitute, loan);
        transfer::public_transfer(borrowed, ADMIN);
        transfer::public_transfer(owner, ADMIN);
        ts::return_shared(registry);
        ts::end(scenario);
    }

    #[test]
    #[expected_failure(abort_code = 0, location = sui_edge::device_registry)]
    fun test_owner_cap_cannot_act_while_the_device_is_borrowed() {
        let mut scenario = ts::begin(ADMIN);
        setup(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
        let device = register(&mut registry, b"busy", &mut scenario);
        let owner = device_registry::activate_device(&mut registry, device, ts::ctx(&mut scenario));
        let (device, loan) = device_registry::borrow_device(&mut registry, &owner);
        device_registry::rotate_device_key(&mut registry, &owner, @0xD2);
        device_registry::return_device(&mut registry, device, loan);
        transfer::public_transfer(owner, ADMIN);
        ts::return_shared(registry);
        ts::end(scenario);
    }

    // L-01 is an arbitrary-state accounting limitation: constructing two
    // same-origin registries requires the test-only initializer. Production
    // exposes no constructor besides the private one-time package init.
    #[test]
    fun test_two_test_only_registries_do_not_confer_identity_authority() {
        let mut scenario = ts::begin(ADMIN);
        setup(&mut scenario);
        ts::next_tx(&mut scenario, ADMIN);
        let first = ts::most_recent_id_shared<DeviceRegistry>().destroy_some();
        device_registry::init_for_testing(ts::ctx(&mut scenario));
        ts::next_tx(&mut scenario, ADMIN);
        let second = ts::most_recent_id_shared<DeviceRegistry>().destroy_some();
        assert!(first != second, 0);
        let mut a = ts::take_shared_by_id<DeviceRegistry>(&scenario, first);
        let mut b = ts::take_shared_by_id<DeviceRegistry>(&scenario, second);
        let clock = clock::create_for_testing(ts::ctx(&mut scenario));
        let da = device_registry::register_device(&mut a, string::utf8(b"a"), DEVICE_ADDR,
            string::utf8(b"aarch64"), &clock, ts::ctx(&mut scenario));
        let db = device_registry::register_device(&mut b, string::utf8(b"b"), DEVICE_ADDR,
            string::utf8(b"aarch64"), &clock, ts::ctx(&mut scenario));
        let id = device_registry::device_id(&da);
        device_registry::deregister_device(&mut b, da, ts::ctx(&mut scenario));
        assert!(device_registry::device_count(&a) == 1, 1);
        assert!(device_registry::device_count(&b) == 0, 2);
        assert!(!device_registry::is_active_signer(&a, id, DEVICE_ADDR), 3);
        assert!(!device_registry::is_active_signer(&b, id, DEVICE_ADDR), 4);
        // Restore the diagnostic counts without claiming an origin binding.
        device_registry::deregister_device(&mut a, db, ts::ctx(&mut scenario));
        clock::destroy_for_testing(clock);
        ts::return_shared(a);
        ts::return_shared(b);
        ts::end(scenario);
    }
}
