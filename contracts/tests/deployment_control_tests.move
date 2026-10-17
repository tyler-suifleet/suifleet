#[test_only]
module sui_edge::deployment_control_tests {
    use std::string;
    use sui::clock;
    use sui::test_scenario as ts;
    use sui_edge::deployment_manager::{Self as dm, DeploymentRecord};
    const ADMIN: address = @0xad;
    fun setup(): ts::Scenario {
        let mut s = ts::begin(ADMIN);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        dm::create_deployment_verified(string::utf8(b"app"), string::utf8(b"1"),
            string::utf8(b"recipe"), string::utf8(b"hash"),
            string::utf8(b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"),
            vector[], vector[object::id_from_address(@0x42)], &clock, ts::ctx(&mut s));
        clock::destroy_for_testing(clock);
        ts::next_tx(&mut s, ADMIN);
        s
    }
    #[test]
    fun existing_record_pause_resume_cancel_and_delete() {
        let mut s = setup();
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        let original = dm::control(&d);
        dm::set_control(&mut d, 1, 10, 100, 20, 80, 5, ts::ctx(&mut s));
        let paused = dm::control(&d);
        assert!(paused != original, 0);
        ts::return_shared(d);
        ts::next_tx(&mut s, ADMIN);
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        assert!(dm::control(&d) == paused, 1);
        dm::set_control(&mut d, 0, 10, 100, 20, 80, 5, ts::ctx(&mut s));
        assert!(dm::control(&d) != paused, 2);
        dm::set_control(&mut d, 2, 10, 100, 20, 80, 5, ts::ctx(&mut s));
        dm::delete_deployment(d, ts::ctx(&mut s));
        ts::end(s);
    }
    #[test, expected_failure(abort_code = 11, location = sui_edge::deployment_manager)]
    fun cancellation_cannot_resume() {
        let mut s = setup();
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        dm::set_control(&mut d, 2, 0, 0, 0, 0, 0, ts::ctx(&mut s));
        dm::set_control(&mut d, 0, 0, 0, 0, 0, 0, ts::ctx(&mut s));
        ts::return_shared(d); ts::end(s);
    }
    #[test, expected_failure(abort_code = 2, location = sui_edge::deployment_manager)]
    fun another_sender_cannot_control() {
        let mut s = setup();
        ts::next_tx(&mut s, @0xbe);
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        dm::set_control(&mut d, 1, 0, 0, 0, 0, 0, ts::ctx(&mut s));
        ts::return_shared(d); ts::end(s);
    }
    #[test, expected_failure(abort_code = 10, location = sui_edge::deployment_manager)]
    fun invalid_window_aborts() {
        let mut s = setup(); let mut d = ts::take_shared<DeploymentRecord>(&s);
        dm::set_control(&mut d, 0, 0, 0, 100, 100, 0, ts::ctx(&mut s));
        ts::return_shared(d); ts::end(s);
    }
    #[test, expected_failure(abort_code = 10, location = sui_edge::deployment_manager)]
    fun invalid_expiry_aborts() {
        let mut s = setup(); let mut d = ts::take_shared<DeploymentRecord>(&s);
        dm::set_control(&mut d, 0, 100, 100, 0, 0, 0, ts::ctx(&mut s));
        ts::return_shared(d); ts::end(s);
    }
}
