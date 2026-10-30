#[test_only]
module sui_edge::subscription_tests {
    use std::string::utf8;
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
        let c = s.take_shared<Customer>();
        let clock = s.take_shared<Clock>();
        let refund = subscription::cancel(&mut account, &c, &clock, s.ctx());
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
        let mut a = s.take_shared<Account<SUI>>(); let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let refund = subscription::cancel(&mut a, &c, &clock, s.ctx());
        transfer::public_transfer(refund, OWNER);
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::customer)]
    fun duplicate_account() { let mut s = setup(false); open(&mut s, 100); open(&mut s, 100); s.end(); }
    #[test, expected_failure(abort_code = 2, location = sui_edge::subscription)]
    fun cannot_collect_cancelled_term() {
        let mut s = setup(true); open(&mut s, 1080);
        let mut a = s.take_shared<Account<SUI>>(); let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let refund = subscription::cancel(&mut a, &c, &clock, s.ctx());
        transfer::public_transfer(refund, OWNER);
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock);
        s.next_tx(OWNER); collect(&mut s, MONTH); s.end();
    }
    #[test]
    fun cancel_settles_started_periods_and_refunds_rest() {
        let mut s = setup(true); open(&mut s, 1080);
        let mut a = s.take_shared<Account<SUI>>(); let c = s.take_shared<Customer>(); let mut clock = s.take_shared<Clock>();
        clock::set_for_testing(&mut clock, 2_628_000_000);
        let refund = subscription::cancel(&mut a, &c, &clock, s.ctx());
        transfer::public_transfer(refund, OWNER);
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock);
        s.next_tx(OWNER);
        let paid = s.take_from_address<CoinType<SUI>>(TREASURY); assert!(paid.value() == 180); coin::burn_for_testing(paid);
        let refund = s.take_from_sender<CoinType<SUI>>(); assert!(refund.value() == 900); coin::burn_for_testing(refund);
        s.end();
    }
    #[test, expected_failure(abort_code = 2, location = sui_edge::subscription)]
    fun plan_change_before_renewal_rejected() {
        let mut s = setup(true); open(&mut s, 2160);
        let mut a = s.take_shared<Account<SUI>>(); let c = s.take_shared<Customer>();
        let clock = s.take_shared<Clock>(); let offer = s.take_immutable<Offer<SUI>>();
        subscription::renew(&mut a, &c, &offer, &clock, s.ctx());
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); ts::return_immutable(offer); s.end();
    }
    #[test]
    fun renewal_after_paid_term_preserves_treasury_and_discount() {
        let mut s = setup(false); open(&mut s, 200); collect(&mut s, 0);
        let mut a = s.take_shared<Account<SUI>>(); let c = s.take_shared<Customer>();
        let mut clock = s.take_shared<Clock>(); let offer = s.take_immutable<Offer<SUI>>();
        clock::set_for_testing(&mut clock, MONTH);
        subscription::renew(&mut a, &c, &offer, &clock, s.ctx());
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); ts::return_immutable(offer);
        s.next_tx(OWNER); collect(&mut s, MONTH); s.end();
    }
    #[test, expected_failure(abort_code = 3, location = sui_edge::subscription)]
    fun renewal_requires_full_prepay() {
        let mut s = setup(false); open(&mut s, 100); collect(&mut s, 0);
        let mut a = s.take_shared<Account<SUI>>(); let c = s.take_shared<Customer>();
        let mut clock = s.take_shared<Clock>(); let offer = s.take_immutable<Offer<SUI>>();
        clock::set_for_testing(&mut clock, MONTH);
        subscription::renew(&mut a, &c, &offer, &clock, s.ctx());
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); ts::return_immutable(offer); s.end();
    }
    #[test, expected_failure(abort_code = 1, location = sui_edge::subscription)]
    fun expired_offer_cannot_be_reused_at_renewal() {
        let mut s = setup(false); open(&mut s, 200); collect(&mut s, 0);
        let mut a = s.take_shared<Account<SUI>>(); let c = s.take_shared<Customer>();
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
        let mut a = s.take_shared<Account<SUI>>(); let c = s.take_shared<Customer>(); let mut clock = s.take_shared<Clock>();
        clock::set_for_testing(&mut clock, MONTH);
        subscription::renew(&mut a, &c, &offer, &clock, s.ctx());
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); ts::return_immutable(offer); ts::return_immutable(old_offer);
        s.next_tx(OWNER); collect(&mut s, MONTH);
        let mut a = s.take_shared<Account<SUI>>(); let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        coin::burn_for_testing(subscription::cancel(&mut a, &c, &clock, s.ctx()));
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
        let mut a = s.take_shared<Account<SUI>>(); let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        subscription::renew(&mut a, &c, &offer, &clock, s.ctx());
        ts::return_shared(a); ts::return_shared(c); ts::return_shared(clock); ts::return_immutable(offer); ts::return_immutable(old_offer); s.end();
    }
    use sui::coin::Coin as CoinType;
    // A local alias is intentionally avoided in the public contract ABI.
}
