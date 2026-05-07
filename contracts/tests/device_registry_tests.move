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
}
