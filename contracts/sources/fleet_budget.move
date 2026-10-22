/// Prepaid fleet network costs (SF-083): an on-chain gas float.
///
/// The fleet owner deposits SUI into a shared `FleetBudget` and holds its
/// `BudgetOwnerCap`. Enrolling a device funds its address with at most the
/// configured float. From then on the device tops itself up only through
/// `draw_gas`, within per-draw, per-period and rate limits that bound what a
/// fully compromised device key can take:
///
/// - SUI already sent to a device cannot be clawed back. A compromised device
///   holds at most its float plus what it drew in the current period; until
///   it is revoked, removed or the budget is paused it can draw at most one
///   period allowance per period.
/// - Draws require the device's signer-bound `DeviceCap` and an active
///   registry identity (custodied, unrevoked, current key), so registry
///   revocation stops them immediately, as do `suspend_device` and `pause`.
/// - Per-device state lives in dynamic fields keyed by device ID, so the
///   budget object itself stays small; each draw is rate limited.
///
/// There is no server, gas station or sponsoring key. Walrus storage is held
/// by the separate `fleet_storage` package, which reads this module's device
/// authorization and pause state.
///
/// Every mutating entry point checks `version`: a later upgrade bumps
/// `VERSION` and calls `migrate`, after which older package versions (which
/// remain callable on Sui) can no longer draw or change the budget.
///
/// Read interface (SF-092): `INTERFACE_VERSION`, the getters at the end of
/// this module and the events below are the stable read surface.
/// `spendable` currently equals `balance` and `reserved` is zero; reserves
/// are added by SF-092 without changing this authority model.
///
/// Introduced by a package upgrade: older package versions cannot reach it.
module sui_edge::fleet_budget {
    use sui::balance::{Self, Balance};
    use sui::clock::Clock;
    use sui::coin::{Self, Coin};
    use sui::dynamic_field as df;
    use sui::event;
    use sui::sui::SUI;
    use sui_edge::device_cap::{Self, DeviceCap};
    use sui_edge::device_registry::{Self, DeviceRegistry, DeviceOwnerCap};

    // ── Constants ─────────────────────────────────────────────────────────────

    /// Package gate for mutating calls; bump on any upgrade that changes them.
    const VERSION: u64 = 1;
    /// Version of the documented read interface (getters and events).
    const INTERFACE_VERSION: u64 = 1;
    /// Upper bound for any single limit, a sanity check against typos
    /// (1,000 SUI); owners set the real limits.
    const MAX_LIMIT_MIST: u64 = 1_000_000_000_000;

    // ── Errors ────────────────────────────────────────────────────────────────

    const EWrongVersion: u64 = 0;
    const ENotOwner: u64 = 1;
    const EWrongRegistry: u64 = 2;
    const EPaused: u64 = 3;
    const ENotEnrolled: u64 = 4;
    const EAlreadyEnrolled: u64 = 5;
    const EInactiveDevice: u64 = 6;
    const ESuspended: u64 = 7;
    const EOverFloat: u64 = 8;
    const EOverAllowance: u64 = 9;
    const ERateLimited: u64 = 10;
    const EInsufficientBalance: u64 = 11;
    const EInvalidLimits: u64 = 12;
    const EZeroAmount: u64 = 13;
    const EOwnerChanged: u64 = 14;
    const ENotUpgrade: u64 = 15;

    // ── State ─────────────────────────────────────────────────────────────────

    /// Draw limits, per device. `float_cap` bounds the initial float and every
    /// single draw; `period_allowance` bounds the total drawn per period;
    /// `max_draws_per_period` and `min_draw_interval_ms` rate-limit draws.
    public struct Limits has copy, drop, store {
        float_cap: u64,
        period_ms: u64,
        period_allowance: u64,
        max_draws_per_period: u64,
        min_draw_interval_ms: u64,
    }

    public struct FleetBudget has key {
        id: UID,
        version: u64,
        /// The registry whose identities may draw.
        registry_id: ID,
        /// The only cap that may manage this budget.
        owner_cap_id: ID,
        balance: Balance<SUI>,
        paused: bool,
        /// Limits given to newly enrolled devices.
        default_limits: Limits,
        /// A `BudgetLow` event is emitted whenever a draw or withdrawal
        /// leaves the balance below this.
        low_balance_threshold: u64,
        device_count: u64,
        total_deposited: u64,
        total_withdrawn: u64,
        /// Floats and draws paid to devices, in total.
        total_drawn: u64,
    }

    public struct BudgetOwnerCap has key, store {
        id: UID,
        budget_id: ID,
    }

    /// Dynamic field on the budget: one device's account.
    public struct DeviceKey has copy, drop, store { device_id: ID }

    public struct DeviceAccount has copy, drop, store {
        /// The device's DeviceOwnerCap at enrollment. A different cap in the
        /// registry identity (re-registration) stops draws.
        owner_cap_id: ID,
        limits: Limits,
        suspended: bool,
        period_start_ms: u64,
        drawn_in_period: u64,
        draws_in_period: u64,
        /// 0 until the first draw.
        last_draw_ms: u64,
        /// Float plus all draws.
        total_drawn: u64,
        enrolled_at_ms: u64,
    }

    // ── Events ────────────────────────────────────────────────────────────────

    public struct BudgetCreated has copy, drop {
        budget_id: ID,
        registry_id: ID,
        owner_cap_id: ID,
        owner: address,
    }

    public struct Deposited has copy, drop {
        budget_id: ID,
        amount: u64,
        balance: u64,
    }

    public struct Withdrawn has copy, drop {
        budget_id: ID,
        amount: u64,
        balance: u64,
    }

    public struct DeviceFunded has copy, drop {
        budget_id: ID,
        device_id: ID,
        device_address: address,
        amount: u64,
        balance: u64,
    }

    /// Every draw a device makes.
    public struct GasDrawn has copy, drop {
        budget_id: ID,
        device_id: ID,
        device_address: address,
        amount: u64,
        drawn_in_period: u64,
        period_allowance: u64,
        draws_in_period: u64,
        max_draws_per_period: u64,
        balance: u64,
        /// The device can draw nothing more this period.
        at_limit: bool,
    }

    /// The balance fell below the owner's threshold (or to zero).
    public struct BudgetLow has copy, drop {
        budget_id: ID,
        balance: u64,
        threshold: u64,
    }

    public struct DeviceStateChanged has copy, drop {
        budget_id: ID,
        device_id: ID,
        enrolled: bool,
        suspended: bool,
        limits: Limits,
    }

    public struct BudgetPaused has copy, drop {
        budget_id: ID,
        paused: bool,
    }

    public struct BudgetConfigured has copy, drop {
        budget_id: ID,
        default_limits: Limits,
        low_balance_threshold: u64,
    }

    // ── Owner ─────────────────────────────────────────────────────────────────

    public fun new_limits(
        float_cap: u64,
        period_ms: u64,
        period_allowance: u64,
        max_draws_per_period: u64,
        min_draw_interval_ms: u64,
    ): Limits {
        let limits = Limits { float_cap, period_ms, period_allowance, max_draws_per_period, min_draw_interval_ms };
        assert_valid(&limits);
        limits
    }

    /// Share a new budget for `registry`, funded with `deposit` (may be zero).
    public fun create(
        registry: &DeviceRegistry,
        deposit: Coin<SUI>,
        default_limits: Limits,
        low_balance_threshold: u64,
        ctx: &mut TxContext,
    ): BudgetOwnerCap {
        assert_valid(&default_limits);
        let id = object::new(ctx);
        let budget_id = id.to_inner();
        let cap = BudgetOwnerCap { id: object::new(ctx), budget_id };
        let amount = deposit.value();
        let budget = FleetBudget {
            id,
            version: VERSION,
            registry_id: object::id(registry),
            owner_cap_id: object::id(&cap),
            balance: deposit.into_balance(),
            paused: false,
            default_limits,
            low_balance_threshold,
            device_count: 0,
            total_deposited: amount,
            total_withdrawn: 0,
            total_drawn: 0,
        };
        event::emit(BudgetCreated { budget_id, registry_id: budget.registry_id, owner_cap_id: budget.owner_cap_id, owner: ctx.sender() });
        if (amount > 0) event::emit(Deposited { budget_id, amount, balance: amount });
        transfer::share_object(budget);
        cap
    }

    public fun deposit(budget: &mut FleetBudget, cap: &BudgetOwnerCap, coin: Coin<SUI>) {
        assert_owner(budget, cap);
        let amount = coin.value();
        assert!(amount > 0, EZeroAmount);
        budget.balance.join(coin.into_balance());
        budget.total_deposited = budget.total_deposited + amount;
        event::emit(Deposited { budget_id: object::id(budget), amount, balance: budget.balance.value() });
    }

    /// Withdraw unused funds. Already-paid floats stay with their devices.
    public fun withdraw(budget: &mut FleetBudget, cap: &BudgetOwnerCap, amount: u64, ctx: &mut TxContext): Coin<SUI> {
        assert_owner(budget, cap);
        assert!(amount > 0, EZeroAmount);
        assert!(budget.balance.value() >= amount, EInsufficientBalance);
        budget.total_withdrawn = budget.total_withdrawn + amount;
        let coin = coin::take(&mut budget.balance, amount, ctx);
        event::emit(Withdrawn { budget_id: object::id(budget), amount, balance: budget.balance.value() });
        check_low(budget);
        coin
    }

    /// Stop (or resume) every draw and storage upload of the fleet at once.
    public fun set_paused(budget: &mut FleetBudget, cap: &BudgetOwnerCap, paused: bool) {
        assert_owner(budget, cap);
        budget.paused = paused;
        event::emit(BudgetPaused { budget_id: object::id(budget), paused });
    }

    public fun configure(budget: &mut FleetBudget, cap: &BudgetOwnerCap, default_limits: Limits, low_balance_threshold: u64) {
        assert_owner(budget, cap);
        assert_valid(&default_limits);
        budget.default_limits = default_limits;
        budget.low_balance_threshold = low_balance_threshold;
        event::emit(BudgetConfigured { budget_id: object::id(budget), default_limits, low_balance_threshold });
    }

    /// Enroll an activated device the owner holds, and send its address an
    /// initial float of `float` (at most the default float cap; zero for none).
    /// Compose after `device_registry::activate_device` in the registration PTB.
    public fun enroll_device(
        budget: &mut FleetBudget,
        cap: &BudgetOwnerCap,
        registry: &DeviceRegistry,
        owner_cap: &DeviceOwnerCap,
        float: u64,
        clock: &Clock,
        ctx: &mut TxContext,
    ) {
        assert_owner(budget, cap);
        assert!(object::id(registry) == budget.registry_id, EWrongRegistry);
        let device_id = device_registry::owner_cap_device_id(owner_cap);
        assert!(!df::exists(&budget.id, DeviceKey { device_id }), EAlreadyEnrolled);
        let identity = device_registry::identity(registry, device_id);
        assert!(identity.is_some(), EInactiveDevice);
        let identity = identity.destroy_some();
        assert!(device_registry::is_custodied(registry, device_id) && !device_registry::identity_revoked(&identity), EInactiveDevice);
        assert!(device_registry::identity_owner_cap(&identity) == object::id(owner_cap), EOwnerChanged);
        let limits = budget.default_limits;
        assert!(float <= limits.float_cap, EOverFloat);
        let now = clock.timestamp_ms();
        let account = DeviceAccount {
            owner_cap_id: object::id(owner_cap),
            limits,
            suspended: false,
            period_start_ms: now,
            drawn_in_period: 0,
            draws_in_period: 0,
            last_draw_ms: 0,
            total_drawn: float,
            enrolled_at_ms: now,
        };
        df::add(&mut budget.id, DeviceKey { device_id }, account);
        budget.device_count = budget.device_count + 1;
        event::emit(DeviceStateChanged { budget_id: object::id(budget), device_id, enrolled: true, suspended: false, limits });
        if (float > 0) pay(budget, device_id, device_registry::identity_signer(&identity), float, ctx);
    }

    /// Owner-paid refill, for a device too empty to pay for its own draw.
    /// At most one float cap per call; does not count toward the period.
    public fun fund_device(
        budget: &mut FleetBudget,
        cap: &BudgetOwnerCap,
        registry: &DeviceRegistry,
        device_id: ID,
        amount: u64,
        ctx: &mut TxContext,
    ) {
        assert_owner(budget, cap);
        assert!(object::id(registry) == budget.registry_id, EWrongRegistry);
        let account = *account(budget, device_id);
        assert!(amount > 0, EZeroAmount);
        assert!(amount <= account.limits.float_cap, EOverFloat);
        let identity = device_registry::identity(registry, device_id);
        assert!(identity.is_some(), EInactiveDevice);
        let identity = identity.destroy_some();
        assert!(!device_registry::identity_revoked(&identity), EInactiveDevice);
        account_mut(budget, device_id).total_drawn = account.total_drawn + amount;
        pay(budget, device_id, device_registry::identity_signer(&identity), amount, ctx);
    }

    public fun set_device_limits(budget: &mut FleetBudget, cap: &BudgetOwnerCap, device_id: ID, limits: Limits) {
        assert_owner(budget, cap);
        assert_valid(&limits);
        let account = account_mut(budget, device_id);
        account.limits = limits;
        let suspended = account.suspended;
        event::emit(DeviceStateChanged { budget_id: object::id(budget), device_id, enrolled: true, suspended, limits });
    }

    /// Stop or resume one device's draws and uploads without touching its
    /// registry identity.
    public fun set_device_suspended(budget: &mut FleetBudget, cap: &BudgetOwnerCap, device_id: ID, suspended: bool) {
        assert_owner(budget, cap);
        let account = account_mut(budget, device_id);
        account.suspended = suspended;
        let limits = account.limits;
        event::emit(DeviceStateChanged { budget_id: object::id(budget), device_id, enrolled: true, suspended, limits });
    }

    /// Remove a device's account (for example after a handover to another
    /// owner); its storage rebate goes to the sender.
    public fun remove_device(budget: &mut FleetBudget, cap: &BudgetOwnerCap, device_id: ID) {
        assert_owner(budget, cap);
        assert!(df::exists(&budget.id, DeviceKey { device_id }), ENotEnrolled);
        let account: DeviceAccount = df::remove(&mut budget.id, DeviceKey { device_id });
        budget.device_count = budget.device_count - 1;
        event::emit(DeviceStateChanged { budget_id: object::id(budget), device_id, enrolled: false, suspended: true, limits: account.limits });
    }

    /// Move the budget to this package version. Older versions then refuse
    /// every mutating call.
    public fun migrate(budget: &mut FleetBudget, cap: &BudgetOwnerCap) {
        assert!(object::id(cap) == budget.owner_cap_id && cap.budget_id == object::id(budget), ENotOwner);
        assert!(budget.version < VERSION, ENotUpgrade);
        budget.version = VERSION;
    }

    // ── Device ────────────────────────────────────────────────────────────────

    /// Top up the sending device's address by `amount`, within its limits.
    /// The sender must be the device's current registry signer and hold its
    /// signer-bound DeviceCap. The coin goes to the sender inside the call, so
    /// a draw is exactly one command the signing service can check.
    #[allow(lint(self_transfer))]
    public fun draw_gas(
        budget: &mut FleetBudget,
        registry: &DeviceRegistry,
        cap: &DeviceCap,
        amount: u64,
        clock: &Clock,
        ctx: &mut TxContext,
    ) {
        assert!(budget.version == VERSION, EWrongVersion);
        let device_id = device_cap::device_id(cap);
        assert_may_draw(budget, registry, cap, ctx);
        assert!(amount > 0, EZeroAmount);
        let now = clock.timestamp_ms();
        let account = account_mut(budget, device_id);
        roll_period(account, now);
        let limits = account.limits;
        assert!(amount <= limits.float_cap, EOverFloat);
        assert!(account.draws_in_period < limits.max_draws_per_period, ERateLimited);
        assert!(account.last_draw_ms == 0 || now >= account.last_draw_ms + limits.min_draw_interval_ms, ERateLimited);
        assert!(account.drawn_in_period + amount <= limits.period_allowance, EOverAllowance);
        account.drawn_in_period = account.drawn_in_period + amount;
        account.draws_in_period = account.draws_in_period + 1;
        account.last_draw_ms = now;
        account.total_drawn = account.total_drawn + amount;
        let drawn_in_period = account.drawn_in_period;
        let draws_in_period = account.draws_in_period;
        assert!(budget.balance.value() >= amount, EInsufficientBalance);
        budget.total_drawn = budget.total_drawn + amount;
        let sender = ctx.sender();
        transfer::public_transfer(coin::take(&mut budget.balance, amount, ctx), sender);
        let balance = budget.balance.value();
        event::emit(GasDrawn {
            budget_id: object::id(budget),
            device_id,
            device_address: sender,
            amount,
            drawn_in_period,
            period_allowance: limits.period_allowance,
            draws_in_period,
            max_draws_per_period: limits.max_draws_per_period,
            balance,
            at_limit: drawn_in_period >= limits.period_allowance || draws_in_period >= limits.max_draws_per_period,
        });
        check_low(budget);
    }

    /// Abort unless the sender may currently draw or upload for `cap`'s
    /// device: current package, budget unpaused, this registry, an enrolled
    /// unsuspended account, the cap's signer and an active registry identity
    /// still bound to the enrolled owner cap. Used by `fleet_storage`.
    public fun assert_may_draw(budget: &FleetBudget, registry: &DeviceRegistry, cap: &DeviceCap, ctx: &TxContext) {
        assert!(budget.version == VERSION, EWrongVersion);
        assert!(!budget.paused, EPaused);
        assert!(object::id(registry) == budget.registry_id, EWrongRegistry);
        let device_id = device_cap::device_id(cap);
        assert!(df::exists(&budget.id, DeviceKey { device_id }), ENotEnrolled);
        let account = df::borrow<DeviceKey, DeviceAccount>(&budget.id, DeviceKey { device_id });
        assert!(!account.suspended, ESuspended);
        device_cap::assert_active(cap, registry, ctx);
        let identity = device_registry::identity(registry, device_id).destroy_some();
        assert!(device_registry::identity_owner_cap(&identity) == account.owner_cap_id, EOwnerChanged);
    }

    /// Abort unless `cap` manages `budget` (for `fleet_storage`).
    public fun assert_owner(budget: &FleetBudget, cap: &BudgetOwnerCap) {
        assert!(budget.version == VERSION, EWrongVersion);
        assert!(object::id(cap) == budget.owner_cap_id && cap.budget_id == object::id(budget), ENotOwner);
    }

    // ── Internal ──────────────────────────────────────────────────────────────

    fun assert_valid(limits: &Limits) {
        assert!(
            limits.period_ms > 0 && limits.max_draws_per_period > 0
                && limits.float_cap <= MAX_LIMIT_MIST && limits.period_allowance <= MAX_LIMIT_MIST,
            EInvalidLimits,
        );
    }

    fun roll_period(account: &mut DeviceAccount, now: u64) {
        if (now >= account.period_start_ms + account.limits.period_ms) {
            account.period_start_ms = now;
            account.drawn_in_period = 0;
            account.draws_in_period = 0;
        };
    }

    fun pay(budget: &mut FleetBudget, device_id: ID, recipient: address, amount: u64, ctx: &mut TxContext) {
        assert!(budget.balance.value() >= amount, EInsufficientBalance);
        budget.total_drawn = budget.total_drawn + amount;
        transfer::public_transfer(coin::take(&mut budget.balance, amount, ctx), recipient);
        event::emit(DeviceFunded { budget_id: object::id(budget), device_id, device_address: recipient, amount, balance: budget.balance.value() });
        check_low(budget);
    }

    fun check_low(budget: &FleetBudget) {
        let balance = budget.balance.value();
        if (balance < budget.low_balance_threshold || balance == 0) {
            event::emit(BudgetLow { budget_id: object::id(budget), balance, threshold: budget.low_balance_threshold });
        };
    }

    fun account(budget: &FleetBudget, device_id: ID): &DeviceAccount {
        assert!(df::exists(&budget.id, DeviceKey { device_id }), ENotEnrolled);
        df::borrow(&budget.id, DeviceKey { device_id })
    }

    fun account_mut(budget: &mut FleetBudget, device_id: ID): &mut DeviceAccount {
        assert!(df::exists(&budget.id, DeviceKey { device_id }), ENotEnrolled);
        df::borrow_mut(&mut budget.id, DeviceKey { device_id })
    }

    // ── Reads (interface version 1) ───────────────────────────────────────────

    public fun interface_version(): u64 { INTERFACE_VERSION }
    public fun package_version(): u64 { VERSION }
    public fun version(budget: &FleetBudget): u64 { budget.version }
    public fun budget_id(cap: &BudgetOwnerCap): ID { cap.budget_id }
    public fun registry_id(budget: &FleetBudget): ID { budget.registry_id }
    public fun owner_cap_id(budget: &FleetBudget): ID { budget.owner_cap_id }
    public fun balance(budget: &FleetBudget): u64 { budget.balance.value() }
    /// Funds devices may draw. Equals `balance` until SF-092 adds reserves.
    public fun spendable(budget: &FleetBudget): u64 { budget.balance.value() }
    /// Funds held back from draws. Zero until SF-092 adds reserves.
    #[allow(lint(unused_object_with_fields))]
    public fun reserved(_budget: &FleetBudget): u64 { 0 }
    public fun paused(budget: &FleetBudget): bool { budget.paused }
    public fun default_limits(budget: &FleetBudget): Limits { budget.default_limits }
    public fun low_balance_threshold(budget: &FleetBudget): u64 { budget.low_balance_threshold }
    public fun device_count(budget: &FleetBudget): u64 { budget.device_count }
    public fun total_deposited(budget: &FleetBudget): u64 { budget.total_deposited }
    public fun total_withdrawn(budget: &FleetBudget): u64 { budget.total_withdrawn }
    public fun total_drawn(budget: &FleetBudget): u64 { budget.total_drawn }
    public fun is_enrolled(budget: &FleetBudget, device_id: ID): bool { df::exists(&budget.id, DeviceKey { device_id }) }
    public fun device_account(budget: &FleetBudget, device_id: ID): Option<DeviceAccount> {
        if (!df::exists(&budget.id, DeviceKey { device_id })) return option::none();
        option::some(*df::borrow(&budget.id, DeviceKey { device_id }))
    }

    public fun limits_float_cap(limits: &Limits): u64 { limits.float_cap }
    public fun limits_period_ms(limits: &Limits): u64 { limits.period_ms }
    public fun limits_period_allowance(limits: &Limits): u64 { limits.period_allowance }
    public fun limits_max_draws_per_period(limits: &Limits): u64 { limits.max_draws_per_period }
    public fun limits_min_draw_interval_ms(limits: &Limits): u64 { limits.min_draw_interval_ms }

    public fun account_owner_cap_id(account: &DeviceAccount): ID { account.owner_cap_id }
    public fun account_limits(account: &DeviceAccount): Limits { account.limits }
    public fun account_suspended(account: &DeviceAccount): bool { account.suspended }
    public fun account_period_start_ms(account: &DeviceAccount): u64 { account.period_start_ms }
    public fun account_drawn_in_period(account: &DeviceAccount): u64 { account.drawn_in_period }
    public fun account_draws_in_period(account: &DeviceAccount): u64 { account.draws_in_period }
    public fun account_last_draw_ms(account: &DeviceAccount): u64 { account.last_draw_ms }
    public fun account_total_drawn(account: &DeviceAccount): u64 { account.total_drawn }
    public fun account_enrolled_at_ms(account: &DeviceAccount): u64 { account.enrolled_at_ms }

    #[test_only]
    public fun set_version_for_testing(budget: &mut FleetBudget, version: u64) { budget.version = version; }
}
