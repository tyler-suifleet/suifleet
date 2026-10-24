#[test_only]
module sui_edge::rollout_health_tests {
    use std::string;
    use sui::clock::{Self, Clock};
    use sui::test_scenario as ts;
    use sui_edge::device_registry::{Self as registry, DeviceRegistry, DeviceOwnerCap};
    use sui_edge::device_cap::{Self, DeviceCap};
    use sui_edge::deployment_manager::{Self as dm, DeploymentRecord};
    const ADMIN: address = @0xad;
    const DEVICE: address = @0xde;

    fun setup(s: &mut ts::Scenario, clock: &Clock): ID {
        registry::init_for_testing(ts::ctx(s));
        ts::next_tx(s, ADMIN);
        let mut r = ts::take_shared<DeviceRegistry>(s);
        let device = registry::register_device(&mut r, string::utf8(b"test"), DEVICE,
            string::utf8(b"linux"), clock, ts::ctx(s));
        let id = object::id(&device);
        transfer::public_transfer(device_cap::issue(&device, ts::ctx(s)), DEVICE);
        transfer::public_transfer(registry::activate_device(&mut r, device, ts::ctx(s)), ADMIN);
        ts::return_shared(r);
        let policy = dm::rollout_policy(1, 1, 1, 100, 1, 10000, 2000);
        let d = dm::new_rollout_verified(string::utf8(b"app"), string::utf8(b"1"), string::utf8(b"blob"),
            string::utf8(b"hash"), string::utf8(b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"),
            vector[], vector[id], vector[0], policy, clock, ts::ctx(s));
        dm::share_deployment(d); id
    }
    fun progress(d: &DeploymentRecord, expected: u8) {
        let (state, _) = dm::rollout_progress(d); assert!(state == expected, 0);
    }
    fun sample(healthy: bool, revoke: bool) {
        let mut s = ts::begin(ADMIN);
        let mut c = clock::create_for_testing(ts::ctx(&mut s)); clock::set_for_testing(&mut c, 1);
        let id = setup(&mut s, &c);
        ts::next_tx(&mut s, ADMIN);
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        let r = ts::take_shared<DeviceRegistry>(&s);
        dm::advance_rollout_onchain(&mut d, &r, vector[id], &c, ts::ctx(&mut s));
        ts::return_shared(r); ts::return_shared(d);
        ts::next_tx(&mut s, DEVICE);
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        let r = ts::take_shared<DeviceRegistry>(&s); let cap = ts::take_from_sender<DeviceCap>(&s);
        dm::report_verified_status(&mut d, &r, &cap, 1, ts::ctx(&mut s));
        ts::return_to_sender(&s, cap); ts::return_shared(r); ts::return_shared(d);
        ts::next_tx(&mut s, ADMIN);
        let mut d = ts::take_shared<DeploymentRecord>(&s); let r = ts::take_shared<DeviceRegistry>(&s);
        // An Applied receipt alone is insufficient, even after the old 120s window.
        clock::set_for_testing(&mut c, 180001);
        dm::advance_rollout_onchain(&mut d, &r, vector[], &c, ts::ctx(&mut s));
        progress(&d, 1); // stall pauses rather than promoting
        dm::control_rollout(&mut d, 0, vector[], string::utf8(b"resume"), &c, ts::ctx(&mut s));
        ts::return_shared(r); ts::return_shared(d); ts::next_tx(&mut s, DEVICE);
        let mut d = ts::take_shared<DeploymentRecord>(&s); let r = ts::take_shared<DeviceRegistry>(&s);
        let cap = ts::take_from_sender<DeviceCap>(&s);
        dm::report_rollout_health(&mut d, &r, &cap, healthy, &c, ts::ctx(&mut s));
        // Retries cannot replace the initial health judgement or increase storage.
        dm::report_rollout_health(&mut d, &r, &cap, !healthy, &c, ts::ctx(&mut s));
        ts::return_to_sender(&s, cap); ts::return_shared(r); ts::return_shared(d);
        ts::next_tx(&mut s, ADMIN);
        let mut d = ts::take_shared<DeploymentRecord>(&s); let mut r = ts::take_shared<DeviceRegistry>(&s);
        if (revoke) {
            let owner = ts::take_from_sender<DeviceOwnerCap>(&s);
            registry::revoke_device(&mut r, &owner); ts::return_to_sender(&s, owner);
        };
        // The same post-apply sample remains valid after the former 120s TTL,
        // including through the bake. Only grant binding and identity matter.
        clock::set_for_testing(&mut c, 360001);
        dm::advance_rollout_onchain(&mut d, &r, vector[], &c, ts::ctx(&mut s));
        clock::set_for_testing(&mut c, 360101);
        if (healthy && !revoke) { dm::advance_rollout_onchain(&mut d, &r, vector[], &c, ts::ctx(&mut s)); };
        progress(&d, if (!healthy) 2 else if (revoke) 1 else 3);
        dm::delete_rollout(d, ts::ctx(&mut s)); ts::return_shared(r);
        clock::destroy_for_testing(c); ts::end(s);
    }
    fun missing_outcome(grant: bool) {
        let mut s = ts::begin(ADMIN);
        let mut c = clock::create_for_testing(ts::ctx(&mut s)); clock::set_for_testing(&mut c, 1);
        let id = setup(&mut s, &c); ts::next_tx(&mut s, ADMIN);
        let mut d = ts::take_shared<DeploymentRecord>(&s); let r = ts::take_shared<DeviceRegistry>(&s);
        if (grant) { dm::advance_rollout_onchain(&mut d, &r, vector[id], &c, ts::ctx(&mut s)); };
        ts::return_shared(d); ts::return_shared(r); ts::next_tx(&mut s, DEVICE);
        let mut d = ts::take_shared<DeploymentRecord>(&s); let r = ts::take_shared<DeviceRegistry>(&s);
        let cap = ts::take_from_sender<DeviceCap>(&s);
        dm::report_rollout_health(&mut d, &r, &cap, true, &c, ts::ctx(&mut s));
        ts::return_shared(d); ts::return_shared(r); ts::return_to_sender(&s, cap);
        clock::destroy_for_testing(c); ts::end(s);
    }
    #[test, expected_failure(abort_code=3, location=sui_edge::deployment_manager)]
    fun ungranted_health_rejected() { missing_outcome(false); }
    #[test, expected_failure(abort_code=3, location=sui_edge::deployment_manager)]
    fun health_before_outcome_rejected() { missing_outcome(true); }
    #[test] fun health_after_grant_is_valid_beyond_wall_clock_window() { sample(true, false); }
    #[test] fun degraded_sample_cannot_be_replaced_by_healthy_retry() { sample(false, false); }
    #[test] fun revoked_identity_cannot_supply_healthy_gate() { sample(true, true); }
}
