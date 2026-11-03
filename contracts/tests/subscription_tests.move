#[test_only]
module sui_edge::subscription_tests {
    use std::string::utf8;
    use sui::bcs;
    use sui::clock::{Self, Clock};
    use sui::coin;
    use sui::sui::SUI;
    use sui::test_scenario::{Self as ts, Scenario};
    use sui_edge::customer::{Self, Customer};
    use sui_edge::device_registry::{Self, DeviceRegistry};
    use sui_edge::subscription::{Self, Account, Offer};

    const OWNER: address = @0xA;
    const TREASURY: address = @0xC;
    const MONTH: u64 = 2_592_000_000;

    fun setup(annual: bool): Scenario {
        let mut s = ts::begin(OWNER);
        device_registry::init_for_testing(s.ctx());
        clock::share_for_testing(clock::create_for_testing(s.ctx()));
        s.next_tx(OWNER);
        let mut r = s.take_shared<DeviceRegistry>();
        let clock = s.take_shared<Clock>();
        customer::create(&mut r, utf8(b"Subscriber"), s.ctx());
        subscription::offer<SUI>(&r, TREASURY, 1, 100, 1000, 100, annual, &clock, s.ctx());
        ts::return_shared(r); ts::return_shared(clock);
        s.next_tx(OWNER);
        s
    }
    fun open(s: &mut Scenario, amount: u64) {
        let mut c = s.take_shared<Customer>();
        let r = s.take_shared<DeviceRegistry>();
        let offer = s.take_immutable<Offer<SUI>>();
        let clock = s.take_shared<Clock>();
        subscription::open(&mut c, &r, &offer, coin::mint_for_testing<SUI>(amount, s.ctx()), &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(r); ts::return_shared(clock); ts::return_immutable(offer);
        s.next_tx(OWNER);
    }
    fun collect(s: &mut Scenario, now: u64) {
        let mut account = s.take_shared<Account<SUI>>();
        let mut c = s.take_shared<Customer>();
        let mut clock = s.take_shared<Clock>();
        clock::set_for_testing(&mut clock, now);
        subscription::collect(&mut account, &mut c, &clock, s.ctx());
        ts::return_shared(account); ts::return_shared(c); ts::return_shared(clock);
        s.next_tx(OWNER);
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun registry_cannot_override_subscription_entitlement() {
        let mut s = setup(true); open(&mut s, 1080);
        let mut c = s.take_shared<Customer>();
        let r = s.take_shared<DeviceRegistry>();
        customer::set_entitlement(&mut c, &r, 999, 9999, 9999999999, s.ctx());
        ts::return_shared(c); ts::return_shared(r); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun subscription_keeps_configured_security_factors() {
        let mut s = setup(true); open(&mut s, 1080);
        let mut c = s.take_shared<Customer>();
        customer::set_security_requirements_for_testing(&mut c, vector[1,0,0,0,0,0,0]);
        let mut account = s.take_shared<Account<SUI>>();
        let clock = s.take_shared<Clock>();
        let refund = subscription::cancel(&mut account, &mut c, &clock, s.ctx());
        coin::burn_for_testing(refund);
        ts::return_shared(c); ts::return_shared(account); ts::return_shared(clock); s.end();
    }
    #[test]
    fun permissionless_collection_and_annual_discount_refund() {
        let mut s = setup(true);
        open(&mut s, 1200);
        s.next_tx(@0xB);
        collect(&mut s, 0);
        let treasury = s.take_from_address<CoinType<SUI>>(TREASURY);
        assert!(coin::value(&treasury) == 90);
        ts::return_to_address(TREASURY, treasury);
        let mut account = s.take_shared<Account<SUI>>();
        let mut c = s.take_shared<Customer>();
        let clock = s.take_shared<Clock>();
        let refund = subscription::cancel(&mut account, &mut c, &clock, s.ctx());
        transfer::public_transfer(refund, OWNER);
        ts::return_shared(account); ts::return_shared(c); ts::return_shared(clock);
        s.next_tx(OWNER);
        let refund = s.take_from_sender<CoinType<SUI>>();
        assert!(coin::value(&refund) == 1110);
        coin::burn_for_testing(refund);
        s.end();
    }
    #[test, expected_failure(abort_code = 2, location = sui_edge::subscription)]
    fun double_collection() {
        let mut s = setup(true); open(&mut s, 1080);
        collect(&mut s, 0); collect(&mut s, 0); s.end();
    }
    #[test, expected_failure(abort_code = 2, location = sui_edge::subscription)]
    fun early_collection() {
        let mut s = setup(true); open(&mut s, 1080);
        collect(&mut s, 0); collect(&mut s, MONTH - 1); s.end();
    }
    #[test, expected_failure(abort_code = 3, location = sui_edge::subscription)]
    fun insufficient_prepay() { let mut s = setup(true); open(&mut s, 1079); s.end(); }
    #[test]
    fun late_collection_never_exceeds_term() {
        let mut s = setup(false); open(&mut s, 100);
        collect(&mut s, 24 * MONTH); s.end();
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::customer)]
    fun outsider_cannot_cancel() {
        let mut s = setup(false); open(&mut s, 100); s.next_tx(@0xB);
        let mut a = s.take_shared<Account<SUI>>(); let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let refund = subscription::cancel(&mut a, &mut c, &clock, s.ctx());
        transfer::public_transfer(refund, OWNER);
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun duplicate_account() { let mut s = setup(false); open(&mut s, 100); open(&mut s, 100); s.end(); }
    #[test, expected_failure(abort_code = 2, location = sui_edge::subscription)]
    fun cannot_collect_cancelled_term() {
        let mut s = setup(true); open(&mut s, 1080);
        let mut a = s.take_shared<Account<SUI>>(); let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let refund = subscription::cancel(&mut a, &mut c, &clock, s.ctx());
        transfer::public_transfer(refund, OWNER);
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock);
        s.next_tx(OWNER); collect(&mut s, MONTH); s.end();
    }
    #[test]
    fun cancel_settles_started_periods_and_refunds_rest() {
        let mut s = setup(true); open(&mut s, 1080);
        let mut a = s.take_shared<Account<SUI>>(); let mut c = s.take_shared<Customer>(); let mut clock = s.take_shared<Clock>();
        clock::set_for_testing(&mut clock, 2_628_000_000);
        let refund = subscription::cancel(&mut a, &mut c, &clock, s.ctx());
        transfer::public_transfer(refund, OWNER);
        let (limit, allowance, used, period) = customer::subscription_entitlement_for_testing(&c);
        assert!(limit == 100 && allowance == 1000 && used == 0 && period == 2 * 2_628_000_000);
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock);
        s.next_tx(OWNER);
        let paid = s.take_from_address<CoinType<SUI>>(TREASURY); assert!(paid.value() == 180); coin::burn_for_testing(paid);
        let refund = s.take_from_sender<CoinType<SUI>>(); assert!(refund.value() == 900); coin::burn_for_testing(refund);
        s.end();
    }
    #[test, expected_failure(abort_code = 2, location = sui_edge::subscription)]
    fun plan_change_before_renewal_rejected() {
        let mut s = setup(true); open(&mut s, 2160);
        let mut a = s.take_shared<Account<SUI>>(); let mut c = s.take_shared<Customer>();
        let clock = s.take_shared<Clock>(); let offer = s.take_immutable<Offer<SUI>>();
        subscription::renew(&mut a, &c, &offer, &clock, s.ctx());
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); ts::return_immutable(offer); s.end();
    }
    #[test]
    fun renewal_after_paid_term_preserves_treasury_and_discount() {
        let mut s = setup(false); open(&mut s, 200); collect(&mut s, 0);
        let mut a = s.take_shared<Account<SUI>>(); let mut c = s.take_shared<Customer>();
        let mut clock = s.take_shared<Clock>(); let offer = s.take_immutable<Offer<SUI>>();
        clock::set_for_testing(&mut clock, MONTH);
        subscription::renew(&mut a, &c, &offer, &clock, s.ctx());
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); ts::return_immutable(offer);
        s.next_tx(OWNER); collect(&mut s, MONTH); s.end();
    }
    #[test, expected_failure(abort_code = 3, location = sui_edge::subscription)]
    fun renewal_requires_full_prepay() {
        let mut s = setup(false); open(&mut s, 100); collect(&mut s, 0);
        let mut a = s.take_shared<Account<SUI>>(); let mut c = s.take_shared<Customer>();
        let mut clock = s.take_shared<Clock>(); let offer = s.take_immutable<Offer<SUI>>();
        clock::set_for_testing(&mut clock, MONTH);
        subscription::renew(&mut a, &c, &offer, &clock, s.ctx());
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); ts::return_immutable(offer); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::subscription)]
    fun expired_offer_cannot_be_reused_at_renewal() {
        let mut s = setup(false); open(&mut s, 200); collect(&mut s, 0);
        let mut a = s.take_shared<Account<SUI>>(); let mut c = s.take_shared<Customer>();
        let mut clock = s.take_shared<Clock>(); let offer = s.take_immutable<Offer<SUI>>();
        clock::set_for_testing(&mut clock, 3 * MONTH);
        subscription::renew(&mut a, &c, &offer, &clock, s.ctx());
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); ts::return_immutable(offer); s.end();
    }
    #[test]
    fun new_tier_and_price_apply_only_at_renewal() {
        let mut s = setup(false); open(&mut s, 300); collect(&mut s, 0);
        let old_offer = s.take_immutable<Offer<SUI>>();
        let r = s.take_shared<DeviceRegistry>(); let clock = s.take_shared<Clock>();
        subscription::offer<SUI>(&r, TREASURY, 2, 200, 2000, 200, false, &clock, s.ctx());
        ts::return_shared(r); ts::return_shared(clock); s.next_tx(OWNER);
        let offer = s.take_immutable<Offer<SUI>>();
        let mut a = s.take_shared<Account<SUI>>(); let mut c = s.take_shared<Customer>(); let mut clock = s.take_shared<Clock>();
        clock::set_for_testing(&mut clock, MONTH);
        subscription::renew(&mut a, &c, &offer, &clock, s.ctx());
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); ts::return_immutable(offer); ts::return_immutable(old_offer);
        s.next_tx(OWNER); collect(&mut s, MONTH);
        let mut a = s.take_shared<Account<SUI>>(); let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        coin::burn_for_testing(subscription::cancel(&mut a, &mut c, &clock, s.ctx()));
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); s.end();
    }
    #[test, expected_failure(abort_code = 4, location = sui_edge::subscription)]
    fun fresh_price_without_notice_is_rejected() {
        let mut s = setup(false); open(&mut s, 300); collect(&mut s, 0);
        let old_offer = s.take_immutable<Offer<SUI>>();
        let r = s.take_shared<DeviceRegistry>(); let mut clock = s.take_shared<Clock>();
        clock::set_for_testing(&mut clock, MONTH);
        subscription::offer<SUI>(&r, TREASURY, 2, 200, 2000, 200, false, &clock, s.ctx());
        ts::return_shared(r); ts::return_shared(clock); s.next_tx(OWNER);
        let offer = s.take_immutable<Offer<SUI>>();
        let mut a = s.take_shared<Account<SUI>>(); let mut c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        subscription::renew(&mut a, &c, &offer, &clock, s.ctx());
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); ts::return_immutable(offer); ts::return_immutable(old_offer); s.end();
    }
    #[test, expected_failure(abort_code = 6, location = sui_edge::customer)]
    fun incompatible_legacy_period_is_rejected_before_custody() {
        let mut s = setup(false);
        let mut c = s.take_shared<Customer>(); let r = s.take_shared<DeviceRegistry>();
        customer::set_entitlement(&mut c, &r, 100, 1000, 18446744073709551615, s.ctx());
        ts::return_shared(c); ts::return_shared(r); s.next_tx(OWNER);
        open(&mut s, 100); s.end();
    }
    #[test]
    fun cancelled_annual_term_can_resubscribe_before_original_end() {
        let mut s = setup(true); open(&mut s, 1080); collect(&mut s, 0);
        let mut a = s.take_shared<Account<SUI>>(); let mut c = s.take_shared<Customer>();
        let mut clock = s.take_shared<Clock>(); let offer = s.take_immutable<Offer<SUI>>();
        clock::set_for_testing(&mut clock, MONTH);
        coin::burn_for_testing(subscription::cancel(&mut a, &mut c, &clock, s.ctx()));
        subscription::deposit(&mut a, coin::mint_for_testing<SUI>(1080, s.ctx()));
        subscription::renew(&mut a, &c, &offer, &clock, s.ctx());
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); ts::return_immutable(offer);
        s.next_tx(OWNER); collect(&mut s, 2_628_000_000); s.end();
    }

    #[test]
    fun secured_cancel_preserves_required_factor() {
        let mut s = setup(true); open(&mut s, 1080);
        let mut c = s.take_shared<Customer>(); let a = s.take_shared<Account<SUI>>();
        customer::set_security_requirements_for_testing(&mut c, vector[1,1,1,1,1,1,1]);
        let clock = s.take_shared<Clock>();
        let arguments = vector[bcs::to_bytes(&b"subscription_cancel"), bcs::to_bytes(&object::id(&c)), bcs::to_bytes(&object::id(&a))];
        customer::queue_security_action(&c, bcs::to_bytes(&arguments), 10000, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(a); ts::return_shared(clock); s.next_tx(OWNER);
        let mut c = s.take_shared<Customer>(); let mut a = s.take_shared<Account<SUI>>();
        let mut action = s.take_shared<sui_edge::security_policy::Action>();
        sui_edge::security_policy::attest(&mut action, 1);
        let clock = s.take_shared<Clock>();
        let refund = subscription::cancel_secured(&mut a, &mut c, &clock, action, s.ctx());
        coin::burn_for_testing(refund);
        ts::return_shared(c); ts::return_shared(a); ts::return_shared(clock); s.end();
    }
    fun queue_open(s: &mut Scenario) {
        let mut c = s.take_shared<Customer>(); let r = s.take_shared<DeviceRegistry>();
        let offer = s.take_immutable<Offer<SUI>>(); let clock = s.take_shared<Clock>();
        customer::set_security_requirements_for_testing(&mut c, vector[1,1,1,1,1,1,1]);
        let payment = coin::mint_for_testing<SUI>(200, s.ctx());
        let arguments = vector[bcs::to_bytes(&b"subscription_open"), bcs::to_bytes(&object::id(&c)),
            bcs::to_bytes(&object::id(&r)), bcs::to_bytes(&object::id(&offer)),
            bcs::to_bytes(&object::id(&payment)), bcs::to_bytes(&payment.value())];
        customer::queue_security_action(&c, bcs::to_bytes(&arguments), clock::timestamp_ms(&clock) + 10000, &clock, s.ctx());
        transfer::public_transfer(payment, OWNER);
        ts::return_shared(c); ts::return_shared(r); ts::return_shared(clock); ts::return_immutable(offer);
        s.next_tx(OWNER);
    }
    fun secured_open(s: &mut Scenario, attest: bool, substitute: bool) {
        let mut c = s.take_shared<Customer>(); let r = s.take_shared<DeviceRegistry>();
        let offer = s.take_immutable<Offer<SUI>>(); let clock = s.take_shared<Clock>();
        let mut action = s.take_shared<sui_edge::security_policy::Action>();
        if (attest) sui_edge::security_policy::attest(&mut action, 1);
        let payment = s.take_from_sender<CoinType<SUI>>();
        let payment = if (substitute) { coin::burn_for_testing(payment); coin::mint_for_testing<SUI>(200, s.ctx()) } else payment;
        subscription::open_secured(&mut c, &r, &offer, payment, &clock, action, s.ctx());
        ts::return_shared(c); ts::return_shared(r); ts::return_shared(clock); ts::return_immutable(offer);
        s.next_tx(OWNER);
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::security_policy)]
    fun secured_open_missing_factor_rejected() {
        let mut s = setup(false); queue_open(&mut s); secured_open(&mut s, false, false); s.end();
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::security_policy)]
    fun secured_open_substituted_payment_rejected() {
        let mut s = setup(false); queue_open(&mut s); secured_open(&mut s, true, true); s.end();
    }
    #[test]
    fun secured_open_and_renew_preserve_factors() {
        let mut s = setup(false); queue_open(&mut s); secured_open(&mut s, true, false);
        collect(&mut s, 0);
        let c = s.take_shared<Customer>(); let a = s.take_shared<Account<SUI>>();
        let offer = s.take_immutable<Offer<SUI>>(); let mut clock = s.take_shared<Clock>();
        clock::set_for_testing(&mut clock, MONTH);
        let arguments = vector[bcs::to_bytes(&b"subscription_renew"), bcs::to_bytes(&object::id(&c)),
            bcs::to_bytes(&object::id(&a)), bcs::to_bytes(&object::id(&offer))];
        customer::queue_security_action(&c, bcs::to_bytes(&arguments), clock::timestamp_ms(&clock) + 10000, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(a); ts::return_shared(clock); ts::return_immutable(offer); s.next_tx(OWNER);
        let mut c = s.take_shared<Customer>(); let mut a = s.take_shared<Account<SUI>>();
        let offer = s.take_immutable<Offer<SUI>>(); let mut clock = s.take_shared<Clock>();
        clock::set_for_testing(&mut clock, MONTH);
        let mut action = s.take_shared<sui_edge::security_policy::Action>();
        sui_edge::security_policy::attest(&mut action, 1);
        subscription::renew_secured(&mut a, &mut c, &offer, &clock, action, s.ctx());
        ts::return_shared(c); ts::return_shared(a); ts::return_shared(clock); ts::return_immutable(offer);
        s.next_tx(OWNER); collect(&mut s, MONTH); s.end();
    }
    use sui::coin::Coin as CoinType;
    // A local alias is intentionally avoided in the public contract ABI.
}
