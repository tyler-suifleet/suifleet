/// Prepaid, fixed-coin terms. No administrator withdrawal or mutable tariff.
module sui_edge::subscription {
    use sui::bcs;
    use sui::balance::{Self, Balance};
    use sui::clock::{Self, Clock};
    use sui::coin::{Self, Coin};
    use sui::event;
    use sui_edge::security_policy::Action;
    use sui_edge::customer::{Self, Customer};
    use sui_edge::device_registry::{Self, DeviceRegistry};

    const EBinding: u64 = 0;
    const BBinding: u8 = 0;
    const ETerms: u64 = 1;
    const BTerms: u8 = 1;
    const ENotDue: u64 = 2;
    const BNotDue: u8 = 2;
    const EFunds: u64 = 3;
    const BFunds: u8 = 3;
    const ENotice: u64 = 4;
    const RATE_9: u8 = 9;
    const RATE_10: u8 = 10;
    const SMALL_0: u64 = 0;
    const SMALL_1: u64 = 1;
    const SMALL_2: u64 = 2;
    const SMALL_7: u64 = 7;
    const SMALL_12: u64 = 12;
    const MONTH_MS: u64 = 2_592_000_000;
    const ANNUAL_PERIOD_MS: u64 = 2_628_000_000;
    const NOTICE_MS: u64 = 2_592_000_000;

    /// Registry authority signs the price by publishing an immutable offer.
    /// T is the exact accepted coin type, including its introduction origin.
    public struct Offer<phantom T> has key {
        id: UID, registry_id: ID, treasury: address, tier: u64,
        device_limit: u64, allowance: u64, fee: u64, periods: u64, period_ms: u64,
        published_at_ms: u64, expires_at_ms: u64,
    }
    /// One persistent account per Customer (including after cancellation).
    public struct Account<phantom T> has key {
        id: UID, customer_id: ID, registry_id: ID, treasury: address,
        balance: Balance<T>, tier: u64, device_limit: u64, allowance: u64,
        fee: u64, remaining: u64, period_ms: u64, next_due_ms: u64, term_end_ms: u64,
        cancelled: bool,
    }
    public struct Deposited has copy, drop { account_id: ID, amount: u64 }
    public struct Collected has copy, drop { account_id: ID, amount: u64, through_ms: u64 }
    public struct PlanChanged has copy, drop { account_id: ID, offer_id: ID, tier: u64, fee: u64, periods: u64 }
    public struct Refunded has copy, drop { account_id: ID, recipient: address, amount: u64 }

    /// Monthly terms are 30 days; annual terms are twelve equal periods over 365 days. The annual
    /// rate is discounted before the immutable offer is created (integer units).
    public fun offer<T>(registry: &DeviceRegistry, treasury: address, tier: u64,
        device_limit: u64, allowance: u64, monthly_fee: u64, annual: bool,
        clock: &Clock, ctx: &mut TxContext) {
        device_registry::assert_admin(registry, ctx);
        q(treasury != @0x0, BTerms); q(monthly_fee > SMALL_0, BTerms); q(device_limit > SMALL_0, BTerms);
        let fee = if (annual) {
            // Round up: never silently truncate a unit from the quoted rate.
            (((monthly_fee as u128) * (RATE_9 as u128) + (RATE_9 as u128)) / (RATE_10 as u128)) as u64
        } else monthly_fee;
        transfer::freeze_object(Offer<T> { id: object::new(ctx), registry_id: object::id(registry),
            treasury, tier, device_limit, allowance, fee, periods: if (annual) SMALL_12 else SMALL_1,
            period_ms: if (annual) ANNUAL_PERIOD_MS else MONTH_MS, published_at_ms: clock::timestamp_ms(clock),
            expires_at_ms: clock::timestamp_ms(clock) + SMALL_2 * NOTICE_MS });
    }

    public fun open<T>(customer: &mut Customer, registry: &DeviceRegistry, offer: &Offer<T>,
        payment: Coin<T>, clock: &Clock, ctx: &mut TxContext) {
        customer::assert_admin(customer, ctx);
        q(offer.registry_id == object::id(registry), BBinding);
        q(offer.device_limit >= customer::active_devices(customer), BTerms);
        let now = clock::timestamp_ms(clock);
        q(now <= offer.expires_at_ms, BTerms);
        let mut account = Account { id: object::new(ctx), customer_id: object::id(customer),
            registry_id: offer.registry_id, treasury: offer.treasury, balance: balance::zero(),
            tier: offer.tier, device_limit: offer.device_limit, allowance: offer.allowance,
            fee: offer.fee, remaining: offer.periods, period_ms: offer.period_ms, next_due_ms: now,
            term_end_ms: now + offer.period_ms * offer.periods, cancelled: false };
        customer::bind_subscription(customer, registry, object::id(&account), now + offer.period_ms);
        deposit(&mut account, payment);
        q(account.balance.value() >= account.fee * account.remaining, BFunds);
        h0(&account, offer);
        transfer::share_object(account);
    }

    public fun deposit<T>(account: &mut Account<T>, payment: Coin<T>) {
        let amount = payment.value();
        account.balance.join(payment.into_balance());
        event::emit(Deposited { account_id: object::id(account), amount });
    }

    /// Permissionless invocation, exactly one period per call. Late calls cannot
    /// charge beyond the purchased term or reset an already collected period.
    public fun collect<T>(account: &mut Account<T>, customer: &mut Customer,
        clock: &Clock, ctx: &mut TxContext) {
        q(object::id(customer) == account.customer_id, BBinding);
        q(!account.cancelled, BNotDue); q(account.remaining > SMALL_0, BNotDue); q(clock::timestamp_ms(clock) >= account.next_due_ms, BNotDue);
        q(account.balance.value() >= account.fee, BFunds);
        let through = account.next_due_ms + account.period_ms;
        customer::subscription_entitlement(customer, object::id(account), account.device_limit, account.allowance, through);
        account.remaining = account.remaining - SMALL_1;
        account.next_due_ms = through;
        transfer::public_transfer(coin::from_balance(account.balance.split(account.fee), ctx), account.treasury);
        event::emit(Collected { account_id: object::id(account), amount: account.fee, through_ms: through });
    }

    /// Only a customer administrator can authorize renewal. Same treasury and
    /// exact coin; an offer must have been public at least 30 days beforehand.
    public fun renew<T>(account: &mut Account<T>, customer: &Customer, offer: &Offer<T>,
        clock: &Clock, ctx: &mut TxContext) {
        customer::assert_admin(customer, ctx);
        q(object::id(customer) == account.customer_id, BBinding); q(offer.registry_id == account.registry_id, BBinding); q(offer.treasury == account.treasury, BBinding);
        let now = clock::timestamp_ms(clock);
        q(account.cancelled || (now >= account.term_end_ms && account.remaining == SMALL_0), BNotDue);
        q(offer.device_limit >= customer::active_devices(customer), BTerms);
        assert!(now >= offer.published_at_ms + NOTICE_MS, ENotice);
        q(now <= offer.expires_at_ms, BTerms);
        q(account.balance.value() >= offer.fee * offer.periods, BFunds);
        account.tier = offer.tier;
        account.device_limit = offer.device_limit;
        account.allowance = offer.allowance;
        account.fee = offer.fee;
        account.remaining = offer.periods;
        account.period_ms = offer.period_ms;
        let start = if (account.cancelled && account.next_due_ms > now) account.next_due_ms else now;
        account.next_due_ms = start;
        account.term_end_ms = start + offer.period_ms * offer.periods;
        account.cancelled = false;
        h0(account, offer);
    }

    /// Cancellation settles every period that has started, even if nobody has
    /// called collect. The rest, including surplus deposits, returns to the
    /// current customer administrator. No admin sweep or cancellation penalty.
    public fun cancel<T>(account: &mut Account<T>, customer: &mut Customer, clock: &Clock, ctx: &mut TxContext): Coin<T> {
        customer::assert_admin(customer, ctx);
        q(object::id(customer) == account.customer_id, BBinding);
        let now = clock::timestamp_ms(clock);
        let due = if (!account.cancelled && now >= account.next_due_ms) {
            let elapsed = (now - account.next_due_ms) / account.period_ms + SMALL_1;
            if (elapsed < account.remaining) elapsed else account.remaining
        } else SMALL_0;
        let earned = due * account.fee;
        q(account.balance.value() >= earned, BFunds);
        if (earned > SMALL_0) {
            let through = account.next_due_ms + due * account.period_ms;
            customer::subscription_entitlement(customer, object::id(account), account.device_limit, account.allowance, through);
            account.next_due_ms = through;
            transfer::public_transfer(coin::from_balance(account.balance.split(earned), ctx), account.treasury);
            event::emit(Collected { account_id: object::id(account), amount: earned, through_ms: account.next_due_ms });
        };
        account.remaining = SMALL_0;
        account.cancelled = true;
        let amount = account.balance.value();
        event::emit(Refunded { account_id: object::id(account), recipient: ctx.sender(), amount });
        coin::from_balance(account.balance.withdraw_all(), ctx)
    }

    public fun open_secured<T>(customer: &mut Customer, registry: &DeviceRegistry, offer: &Offer<T>,
        payment: Coin<T>, clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[bcs::to_bytes(&object::id(registry)), bcs::to_bytes(&object::id(offer)),
            bcs::to_bytes(&object::id(&payment)), bcs::to_bytes(&payment.value())];
        customer::begin_intent(customer, action, b"subscription_open", arguments, SMALL_7, false, clock, ctx);
        open(customer, registry, offer, payment, clock, ctx);
        customer::finish_security_action(customer);
    }

    public fun renew_secured<T>(account: &mut Account<T>, customer: &mut Customer, offer: &Offer<T>,
        clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[bcs::to_bytes(&object::id(account)), bcs::to_bytes(&object::id(offer))];
        customer::begin_intent(customer, action, b"subscription_renew", arguments, SMALL_7, false, clock, ctx);
        renew(account, customer, offer, clock, ctx);
        customer::finish_security_action(customer);
    }

    public fun cancel_secured<T>(account: &mut Account<T>, customer: &mut Customer,
        clock: &Clock, action: Action, ctx: &mut TxContext): Coin<T> {
        let arguments = vector[bcs::to_bytes(&object::id(account))];
        customer::begin_intent(customer, action, b"subscription_cancel", arguments, SMALL_7, false, clock, ctx);
        let refund = cancel(account, customer, clock, ctx);
        customer::finish_security_action(customer);
        refund
    }

    /// Private helper: emit_plan.
    fun h0<T>(account: &Account<T>, offer: &Offer<T>) {
        event::emit(PlanChanged { account_id: object::id(account), offer_id: object::id(offer),
            tier: account.tier, fee: account.fee, periods: account.remaining });
    }

    // Private named guards retain module-local error codes; selectors are fixed at each call.

    fun q(condition: bool, code: u8) {
        if (!condition) {
            if (code == BTerms) abort ETerms;
            if (code == BBinding) abort EBinding;
            if (code == BFunds) abort EFunds;
            abort ENotDue
        }
    }
}
