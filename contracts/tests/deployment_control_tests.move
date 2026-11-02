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

    // ── SF-174 recurring weekly windows ──────────────────────────────────────
    fun berlin(d: &mut DeploymentRecord, s: &mut ts::Scenario) {
        // Sunday 02:00 for two hours, plus weekdays 23:30 crossing midnight.
        dm::set_recurring_window(d, string::utf8(b"Europe/Berlin"), vector[64, 31],
            vector[120, 1410], vector[120, 90], ts::ctx(s));
    }
    #[test]
    fun recurring_window_is_additive_bumps_control_and_is_deleted() {
        let mut s = setup();
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        assert!(dm::recurring_window(&d).is_none(), 0);
        // An absent Control becomes an explicit immediate one at revision 1.
        berlin(&mut d, &mut s);
        let c = dm::control(&d);
        let (revision, timezone, days, starts, durations) = dm::recurring_window_fields(&dm::recurring_window(&d).destroy_some());
        assert!(revision == 1 && timezone == string::utf8(b"Europe/Berlin"), 1);
        assert!(days == vector[64, 31] && starts == vector[120, 1410] && durations == vector[120, 90], 2);
        dm::set_control(&mut d, 1, 10, 100, 20, 80, 5, ts::ctx(&mut s));
        assert!(dm::control(&d) != c, 3);
        ts::return_shared(d);
        ts::next_tx(&mut s, ADMIN);
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        // Replacing restarts notice: the Control revision follows every change,
        // and the other control fields are preserved.
        let before = dm::control(&d);
        dm::set_recurring_window(&mut d, string::utf8(b"America/New_York"), vector[127], vector[0], vector[1440], ts::ctx(&mut s));
        let (revision, timezone, _, _, _) = dm::recurring_window_fields(&dm::recurring_window(&d).destroy_some());
        assert!(revision == 3 && timezone == string::utf8(b"America/New_York"), 4);
        assert!(dm::control(&d) != before && dm::scheduled_start_ms(&d) == 10, 5);
        dm::clear_recurring_window(&mut d, ts::ctx(&mut s));
        assert!(dm::recurring_window(&d).is_none(), 6);
        berlin(&mut d, &mut s);
        let (revision, _, _, _, _) = dm::recurring_window_fields(&dm::recurring_window(&d).destroy_some());
        assert!(revision == 5, 7);
        // Deletion removes the field together with the Control.
        dm::delete_deployment(d, ts::ctx(&mut s));
        ts::end(s);
    }
    #[test]
    fun recurring_window_accepts_bounds_and_reserved_sentinel() {
        let mut s = setup();
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        dm::set_recurring_window(&mut d, string::utf8(b"device-local"), vector[1, 2, 4, 8, 16, 32, 64, 127],
            vector[0, 1439, 0, 0, 0, 0, 0, 0], vector[1, 1440, 1, 1, 1, 1, 1, 1], ts::ctx(&mut s));
        dm::set_recurring_window(&mut d, string::utf8(b"Etc/GMT+5"), vector[1], vector[0], vector[1], ts::ctx(&mut s));
        dm::set_recurring_window(&mut d, string::utf8(b"America/Argentina/ComodRivadavia"), vector[1], vector[0], vector[1], ts::ctx(&mut s));
        ts::return_shared(d); ts::end(s);
    }
    #[test, expected_failure(abort_code = 2, location = sui_edge::deployment_manager)]
    fun another_sender_cannot_set_recurring_window() {
        let mut s = setup();
        ts::next_tx(&mut s, @0xbe);
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        berlin(&mut d, &mut s);
        ts::return_shared(d); ts::end(s);
    }
    #[test, expected_failure(abort_code = 2, location = sui_edge::deployment_manager)]
    fun another_sender_cannot_clear_recurring_window() {
        let mut s = setup();
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        berlin(&mut d, &mut s);
        ts::return_shared(d);
        ts::next_tx(&mut s, @0xbe);
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        dm::clear_recurring_window(&mut d, ts::ctx(&mut s));
        ts::return_shared(d); ts::end(s);
    }
    #[test, expected_failure(abort_code = 11, location = sui_edge::deployment_manager)]
    fun cancelled_deployment_rejects_recurring_window() {
        let mut s = setup();
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        dm::set_control(&mut d, 2, 0, 0, 0, 0, 0, ts::ctx(&mut s));
        berlin(&mut d, &mut s);
        ts::return_shared(d); ts::end(s);
    }
    #[test, expected_failure(abort_code = 11, location = sui_edge::deployment_manager)]
    fun cancelled_deployment_keeps_recurring_window() {
        let mut s = setup();
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        berlin(&mut d, &mut s);
        dm::set_control(&mut d, 2, 0, 0, 0, 0, 0, ts::ctx(&mut s));
        dm::clear_recurring_window(&mut d, ts::ctx(&mut s));
        ts::return_shared(d); ts::end(s);
    }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun clearing_absent_recurring_window_aborts() {
        let mut s = setup();
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        dm::clear_recurring_window(&mut d, ts::ctx(&mut s));
        ts::return_shared(d); ts::end(s);
    }
    fun invalid(timezone: vector<u8>, days: vector<u8>, starts: vector<u16>, durations: vector<u16>) {
        let mut s = setup();
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        dm::set_recurring_window(&mut d, string::utf8(timezone), days, starts, durations, ts::ctx(&mut s));
        ts::return_shared(d); ts::end(s);
    }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun no_slots_abort() { invalid(b"UTC", vector[], vector[], vector[]) }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun nine_slots_abort() { invalid(b"UTC", vector[1, 1, 1, 1, 1, 1, 1, 1, 1], vector[0, 0, 0, 0, 0, 0, 0, 0, 0], vector[1, 1, 1, 1, 1, 1, 1, 1, 1]) }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun mismatched_slot_vectors_abort() { invalid(b"UTC", vector[1, 2], vector[0], vector[1, 1]) }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun empty_day_mask_aborts() { invalid(b"UTC", vector[0], vector[0], vector[1]) }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun eighth_day_bit_aborts() { invalid(b"UTC", vector[128], vector[0], vector[1]) }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun start_after_last_minute_aborts() { invalid(b"UTC", vector[1], vector[1440], vector[1]) }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun zero_duration_aborts() { invalid(b"UTC", vector[1], vector[0], vector[0]) }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun duration_over_a_day_aborts() { invalid(b"UTC", vector[1], vector[0], vector[1441]) }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun empty_timezone_aborts() { invalid(b"", vector[1], vector[0], vector[1]) }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun long_timezone_aborts() { invalid(b"Aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", vector[1], vector[0], vector[1]) }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun path_timezone_aborts() { invalid(b"/etc/localtime", vector[1], vector[0], vector[1]) }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun dotted_timezone_aborts() { invalid(b"Europe/../Berlin", vector[1], vector[0], vector[1]) }
    #[test, expected_failure(abort_code = 18, location = sui_edge::deployment_manager)]
    fun spaced_timezone_aborts() { invalid(b"Europe/Berlin ", vector[1], vector[0], vector[1]) }
}
