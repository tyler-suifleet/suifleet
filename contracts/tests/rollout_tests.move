#[test_only]
module sui_edge::rollout_tests {
    use std::string;
    use sui::clock::{Self, Clock};
    use sui::test_scenario as ts;
    use sui_edge::deployment_manager::{Self as dm, DeploymentRecord};
    const ADMIN: address = @0xad;
    fun id(n: address): ID { object::id_from_address(n) }
    fun setup(s: &mut ts::Scenario, clock: &Clock, rate: u64, concurrency: u64, minimum: u64, threshold: u64): DeploymentRecord {
        let p = dm::rollout_policy(concurrency, rate, 1000, 100, minimum, threshold, 2000);
        dm::new_rollout_verified(string::utf8(b"app"), string::utf8(b"1"), string::utf8(b"recipe"),
            string::utf8(b"hash"), string::utf8(b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"),
            vector[], vector[id(@0x1), id(@0x2), id(@0x3), id(@0x4)], vector[0,0,1,1], p, clock, ts::ctx(s))
    }
    fun progress(d: &DeploymentRecord, state: u8, cohort: u64): bool {
        let (actual_state, actual_cohort) = dm::rollout_progress(d);
        actual_state == state && actual_cohort == cohort
    }
    #[test]
    fun multi_device_rate_concurrency_bake_restart_and_health() {
        let mut s = ts::begin(ADMIN);
        let mut c = clock::create_for_testing(ts::ctx(&mut s)); clock::set_for_testing(&mut c, 1);
        let mut d = setup(&mut s, &c, 1, 1, 1, 10000);
        let a = id(@0x1); let b = id(@0x2); let x = id(@0x3); let y = id(@0x4);
        assert!(!dm::rollout_eligible(&d, a), 0);
        dm::advance_rollout(&mut d, vector[a,b,x,y], vector[], vector[], &c, ts::ctx(&mut s));
        assert!(dm::rollout_eligible(&d, a) && !dm::rollout_eligible(&d, b) && !dm::rollout_eligible(&d, x), 1);
        clock::set_for_testing(&mut c, 1001);
        dm::advance_rollout(&mut d, vector[a,b,x,y], vector[], vector[], &c, ts::ctx(&mut s));
        assert!(!dm::rollout_eligible(&d, b), 2); // rate ready, concurrency full
        dm::rollout_receipt_for_testing(&mut d, a, 1);
        dm::advance_rollout(&mut d, vector[a,b,x,y], vector[a], vector[], &c, ts::ctx(&mut s));
        assert!(dm::rollout_eligible(&d, b), 3);
        dm::rollout_receipt_for_testing(&mut d, b, 1);
        clock::set_for_testing(&mut c, 1002);
        dm::advance_rollout(&mut d, vector[a,b,x,y], vector[a,b], vector[], &c, ts::ctx(&mut s));
        dm::share_deployment(d); ts::next_tx(&mut s, ADMIN);
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        clock::set_for_testing(&mut c, 1050);
        dm::advance_rollout(&mut d, vector[a,b,x,y], vector[a], vector[], &c, ts::ctx(&mut s));
        clock::set_for_testing(&mut c, 1102);
        dm::advance_rollout(&mut d, vector[a,b,x,y], vector[a,b], vector[], &c, ts::ctx(&mut s));
        assert!(progress(&d,0,0), 4); // missing health restarted bake
        clock::set_for_testing(&mut c, 1202);
        dm::advance_rollout(&mut d, vector[a,b,x,y], vector[a,b], vector[], &c, ts::ctx(&mut s));
        assert!(progress(&d,0,1) && !dm::rollout_eligible(&d, x), 5);
        clock::set_for_testing(&mut c, 2001);
        dm::advance_rollout(&mut d, vector[x,y], vector[a,b], vector[], &c, ts::ctx(&mut s));
        assert!(dm::rollout_eligible(&d, x) && !dm::rollout_eligible(&d, y), 6);
        dm::delete_rollout(d, ts::ctx(&mut s)); clock::destroy_for_testing(c); ts::end(s);
    }
    #[test]
    fun offline_timeout_retains_capacity_and_requires_audited_recovery() {
        let mut s = ts::begin(ADMIN); let mut c = clock::create_for_testing(ts::ctx(&mut s));
        clock::set_for_testing(&mut c, 1); let mut d = setup(&mut s, &c, 4, 1, 1, 10000);
        let a = id(@0x1); let b = id(@0x2);
        dm::advance_rollout(&mut d, vector[a], vector[], vector[], &c, ts::ctx(&mut s));
        clock::set_for_testing(&mut c, 2001);
        dm::advance_rollout(&mut d, vector[b], vector[], vector[], &c, ts::ctx(&mut s));
        assert!(progress(&d,1,0) && !dm::rollout_eligible(&d,b), 0);
        dm::control_rollout(&mut d, 0, vector[], string::utf8(b"device reconnected"), &c, ts::ctx(&mut s));
        dm::advance_rollout(&mut d, vector[b], vector[], vector[], &c, ts::ctx(&mut s));
        assert!(dm::rollout_eligible(&d,a) && !dm::rollout_eligible(&d,b), 1);
        dm::delete_rollout(d, ts::ctx(&mut s)); clock::destroy_for_testing(c); ts::end(s);
    }
    #[test]
    fun minimum_sample_and_failure_threshold_are_inclusive_and_sticky() {
        let mut s = ts::begin(ADMIN); let mut c = clock::create_for_testing(ts::ctx(&mut s));
        clock::set_for_testing(&mut c, 1); let mut d = setup(&mut s, &c, 4, 4, 2, 5000);
        let a = id(@0x1); let b = id(@0x2);
        dm::advance_rollout(&mut d, vector[a,b], vector[], vector[], &c, ts::ctx(&mut s));
        dm::rollout_receipt_for_testing(&mut d,a,2);
        dm::advance_rollout(&mut d, vector[a,b], vector[], vector[], &c, ts::ctx(&mut s));
        assert!(progress(&d,0,0), 0);
        dm::rollout_receipt_for_testing(&mut d,a,1); // cannot erase observed failure
        dm::rollout_receipt_for_testing(&mut d,b,1);
        dm::advance_rollout(&mut d, vector[a,b], vector[b], vector[], &c, ts::ctx(&mut s));
        assert!(progress(&d,2,0) && !dm::rollout_eligible(&d,b), 1);
        dm::delete_rollout(d, ts::ctx(&mut s)); clock::destroy_for_testing(c); ts::end(s);
    }
    #[test, expected_failure(abort_code=2, location=sui_edge::deployment_manager)]
    fun non_creator_cannot_grant() {
        let mut s = ts::begin(ADMIN); let c = clock::create_for_testing(ts::ctx(&mut s));
        let d = setup(&mut s,&c,4,1,1,10000); dm::share_deployment(d);
        ts::next_tx(&mut s,@0xbe); let mut d = ts::take_shared<DeploymentRecord>(&s);
        dm::advance_rollout(&mut d,vector[id(@0x1)],vector[],vector[],&c,ts::ctx(&mut s));
        ts::return_shared(d); clock::destroy_for_testing(c); ts::end(s);
    }
    #[test, expected_failure(abort_code=13, location=sui_edge::deployment_manager)]
    fun admitted_offline_member_cannot_be_skipped() {
        let mut s = ts::begin(ADMIN); let mut c = clock::create_for_testing(ts::ctx(&mut s));
        clock::set_for_testing(&mut c,1); let mut d = setup(&mut s,&c,4,1,1,10000);
        dm::advance_rollout(&mut d,vector[id(@0x1)],vector[],vector[],&c,ts::ctx(&mut s));
        dm::control_rollout(&mut d,0,vector[id(@0x1)],string::utf8(b"offline"),&c,ts::ctx(&mut s));
        dm::delete_rollout(d,ts::ctx(&mut s)); clock::destroy_for_testing(c); ts::end(s);
    }
    #[test]
    fun degraded_health_counts_as_failure() {
        let mut s = ts::begin(ADMIN); let mut c = clock::create_for_testing(ts::ctx(&mut s));
        clock::set_for_testing(&mut c, 1); let mut d = setup(&mut s, &c, 4, 4, 1, 5000);
        let a = id(@0x1); let b = id(@0x2);
        dm::advance_rollout(&mut d, vector[a,b], vector[], vector[], &c, ts::ctx(&mut s));
        dm::rollout_receipt_for_testing(&mut d,a,1); dm::rollout_receipt_for_testing(&mut d,b,1);
        dm::advance_rollout(&mut d, vector[a,b], vector[a,b], vector[], &c, ts::ctx(&mut s));
        clock::set_for_testing(&mut c, 101);
        dm::advance_rollout(&mut d, vector[a,b], vector[b], vector[a], &c, ts::ctx(&mut s));
        assert!(progress(&d,2,0), 0);
        dm::delete_rollout(d,ts::ctx(&mut s)); clock::destroy_for_testing(c); ts::end(s);
    }
    #[test, expected_failure(abort_code=3, location=sui_edge::deployment_manager)]
    fun ungranted_outcome_is_rejected() {
        let mut s = ts::begin(ADMIN); let c = clock::create_for_testing(ts::ctx(&mut s));
        let mut d = setup(&mut s,&c,4,1,1,10000);
        dm::rollout_receipt_for_testing(&mut d,id(@0x1),1);
        dm::delete_rollout(d,ts::ctx(&mut s)); clock::destroy_for_testing(c); ts::end(s);
    }
    #[test, expected_failure(abort_code=13, location=sui_edge::deployment_manager)]
    fun manual_abort_cannot_resume() {
        let mut s = ts::begin(ADMIN); let c = clock::create_for_testing(ts::ctx(&mut s));
        let mut d = setup(&mut s,&c,4,1,1,10000);
        dm::control_rollout(&mut d,2,vector[],string::utf8(b"operator abort"),&c,ts::ctx(&mut s));
        dm::control_rollout(&mut d,0,vector[],string::utf8(b"resume denied"),&c,ts::ctx(&mut s));
        dm::delete_rollout(d,ts::ctx(&mut s)); clock::destroy_for_testing(c); ts::end(s);
    }

}
