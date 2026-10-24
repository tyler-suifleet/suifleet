/// Prepaid fleet network costs (SF-083): an on-chain gas float.
///
/// The fleet owner deposits SUI into a shared `FleetBudget` and holds its
/// `BudgetOwnerCap`. Enrolling a device funds its address with at most the
/// configured float and creates the device's own shared `GasAccount`, holding
/// a reserve the owner allocates from the budget. From then on the device
/// tops itself up only through `draw_gas`, from its own account, within
/// per-draw, per-period and rate limits that bound what a fully compromised
/// device key can take:
///
/// - SUI already sent to a device cannot be clawed back. A compromised device
///   holds at most its float plus what it drew in the current period; until
///   it is revoked, removed or the budget is paused it can draw at most one
///   period allowance per period, and never more than its account's reserve.
///   The owner can take an unused reserve back at any time.
/// - Draws require the device's signer-bound `DeviceCap` and an active
///   registry identity (custodied, unrevoked, current key), so registry
///   revocation stops them immediately, as do `suspend_device`,
///   `remove_device` and `pause`.
/// - Draws only read the fleet-wide `FleetBudget` (`&FleetBudget`): its pause
///   flag, version and device index. They write only the drawing device's
///   `GasAccount`, so devices do not serialize on one fleet object, and a
///   device's draws never change the budget other devices and the owner use.
///
/// There is no server, gas station or sponsoring key. Walrus storage is held
/// by the separate `fleet_storage` package, which reads this module's device
/// authorization and pause state.
///
/// Every mutating entry point checks the budget's `version`: a later upgrade
/// bumps `VERSION` and calls `migrate`, after which older package versions
/// (which remain callable on Sui) can no longer draw or change the budget or
/// any of its accounts.
///
/// Read interface (SF-092): `INTERFACE_VERSION`, the getters at the end of
/// this module and the events below are the stable read surface.
/// `spendable` currently equals the unallocated `balance` and `reserved` is
/// zero; reserves are added by SF-092 without changing this authority model.
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
    const MANAGED_VERSION: u64 = 2;
    /// Version of the documented read interface (getters and events).
    const INTERFACE_VERSION: u64 = 1;
    /// Upper bound for any single limit or reserve, a sanity check against
    /// typos (1,000 SUI); owners set the real limits.
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
    const EWrongAccount: u64 = 16;
    const EOverReserve: u64 = 17;

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
        /// Funds not yet allocated to a device account.
        balance: Balance<SUI>,
        paused: bool,
        /// Limits given to newly enrolled devices.
        default_limits: Limits,
        /// A `BudgetLow` event is emitted whenever an owner operation leaves
        /// the balance below this.
        low_balance_threshold: u64,
        device_count: u64,
        total_deposited: u64,
        total_withdrawn: u64,
        /// Floats and owner refills paid straight to device addresses.
        total_paid: u64,
        /// Moved into device accounts' reserves, in total.
        total_allocated: u64,
        /// Moved back from device accounts' reserves, in total.
        total_reclaimed: u64,
    }

    public struct BudgetOwnerCap has key, store {
        id: UID,
        budget_id: ID,
    }

    /// Dynamic field on the budget: the device's current `GasAccount` ID.
    /// Removing it (or re-enrolling) retires the old account at once.
    public struct CustomerManagedKey has copy, drop, store {}

    public struct DeviceKey has copy, drop, store { device_id: ID }

    /// One device's shared account: its limits, usage and the reserve it
    /// draws from. Only its own draws and the owner write it.
    public struct GasAccount has key {
        id: UID,
        budget_id: ID,
        device_id: ID,
        /// The device's DeviceOwnerCap at enrollment. A different cap in the
        /// registry identity (re-registration) stops draws.
        owner_cap_id: ID,
        reserve: Balance<SUI>,
        limits: Limits,
        suspended: bool,
        /// Set by `remove_device`; a closed account never draws again.
        closed: bool,
        period_start_ms: u64,
        drawn_in_period: u64,
        draws_in_period: u64,
        /// 0 until the first draw.
        last_draw_ms: u64,
        /// Float, draws and owner refills paid to the device, in total.
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

    /// Float or owner refill paid straight to a device address.
    public struct DeviceFunded has copy, drop {
        budget_id: ID,
        device_id: ID,
        device_address: address,
        amount: u64,
        balance: u64,
    }

    /// The owner moved funds between the budget and a device's reserve.
    public struct ReserveChanged has copy, drop {
        budget_id: ID,
        device_id: ID,
        account_id: ID,
        /// Moved into the reserve (true) or back to the budget (false).
        allocated: bool,
        amount: u64,
        reserve: u64,
        balance: u64,
    }

    /// Every draw a device makes.
    public struct GasDrawn has copy, drop {
        budget_id: ID,
        device_id: ID,
        account_id: ID,
        device_address: address,
        amount: u64,
        drawn_in_period: u64,
        period_allowance: u64,
        draws_in_period: u64,
        max_draws_per_period: u64,
        /// The account's reserve left after this draw.
        reserve: u64,
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
        account_id: ID,
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
        let (budget, cap) = new_budget(registry, deposit, default_limits, low_balance_threshold, ctx);
        transfer::share_object(budget);
        cap
    }

    public(package) fun share_managed(budget: FleetBudget) { transfer::share_object(budget); }

    public(package) fun new_budget(registry: &DeviceRegistry, deposit: Coin<SUI>,
        default_limits: Limits, low_balance_threshold: u64, ctx: &mut TxContext): (FleetBudget, BudgetOwnerCap) {
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
            total_paid: 0,
            total_allocated: 0,
            total_reclaimed: 0,
        };
        event::emit(BudgetCreated { budget_id, registry_id: budget.registry_id, owner_cap_id: budget.owner_cap_id, owner: ctx.sender() });
        if (amount > 0) event::emit(Deposited { budget_id, amount, balance: amount });
        (budget, cap)
    }

    public fun deposit(budget: &mut FleetBudget, cap: &BudgetOwnerCap, coin: Coin<SUI>) {
        assert_owner(budget, cap);
        let amount = coin.value();
        assert!(amount > 0, EZeroAmount);
        budget.balance.join(coin.into_balance());
        budget.total_deposited = budget.total_deposited + amount;
        event::emit(Deposited { budget_id: object::id(budget), amount, balance: budget.balance.value() });
    }

    /// Withdraw unallocated funds. Already-paid floats stay with their
    /// devices; take unused reserves back first with `reclaim_reserve`.
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

    /// Enroll an activated device the owner holds: share its `GasAccount`
    /// with `reserve` from the budget, and send its address an initial float
    /// of `float` (at most the default float cap; zero for none). Compose
    /// after `device_registry::activate_device` in the registration PTB.
    /// Returns the account's ID.
    public fun enroll_device(
        budget: &mut FleetBudget,
        cap: &BudgetOwnerCap,
        registry: &DeviceRegistry,
        owner_cap: &DeviceOwnerCap,
        float: u64,
        reserve: u64,
        clock: &Clock,
        ctx: &mut TxContext,
    ): ID {
        assert!(budget.version == VERSION, EWrongVersion);
        enroll_internal(budget, cap, registry, owner_cap, float, reserve, clock, ctx)
    }

    public(package) fun enroll_managed(
        budget: &mut FleetBudget, cap: &BudgetOwnerCap, registry: &DeviceRegistry,
        owner_cap: &DeviceOwnerCap, float: u64, reserve: u64, clock: &Clock, ctx: &mut TxContext,
    ): ID {
        assert!(budget.version == MANAGED_VERSION, EWrongVersion);
        enroll_internal(budget, cap, registry, owner_cap, float, reserve, clock, ctx)
    }

    fun enroll_internal(
        budget: &mut FleetBudget, cap: &BudgetOwnerCap, registry: &DeviceRegistry,
        owner_cap: &DeviceOwnerCap, float: u64, reserve: u64, clock: &Clock, ctx: &mut TxContext,
    ): ID {
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
        assert!(reserve <= MAX_LIMIT_MIST, EOverReserve);
        let now = clock.timestamp_ms();
        let budget_id = object::id(budget);
        let mut account = GasAccount {
            id: object::new(ctx),
            budget_id,
            device_id,
            owner_cap_id: object::id(owner_cap),
            reserve: balance::zero(),
            limits,
            suspended: false,
            closed: false,
            period_start_ms: now,
            drawn_in_period: 0,
            draws_in_period: 0,
            last_draw_ms: 0,
            total_drawn: float,
            enrolled_at_ms: now,
        };
        let account_id = object::id(&account);
        df::add(&mut budget.id, DeviceKey { device_id }, account_id);
        budget.device_count = budget.device_count + 1;
        event::emit(DeviceStateChanged { budget_id, device_id, account_id, enrolled: true, suspended: false, limits });
        if (reserve > 0) allocate(budget, &mut account, reserve);
        if (float > 0) pay(budget, device_id, device_registry::identity_signer(&identity), float, ctx);
        transfer::share_object(account);
        account_id
    }

    /// Move `amount` from the budget into a device's reserve, for its draws.
    public fun fund_reserve(budget: &mut FleetBudget, cap: &BudgetOwnerCap, account: &mut GasAccount, amount: u64) {
        assert_owner(budget, cap);
        assert_current(budget, account);
        assert!(amount > 0, EZeroAmount);
        assert!(account.reserve.value() + amount <= MAX_LIMIT_MIST, EOverReserve);
        allocate(budget, account, amount);
    }

    /// Move `amount` of a device's unused reserve back into the budget.
    /// Works on a removed (closed) account too.
    public fun reclaim_reserve(budget: &mut FleetBudget, cap: &BudgetOwnerCap, account: &mut GasAccount, amount: u64) {
        assert_owner(budget, cap);
        assert!(account.budget_id == object::id(budget), EWrongAccount);
        assert!(amount > 0, EZeroAmount);
        assert!(account.reserve.value() >= amount, EInsufficientBalance);
        reclaim(budget, account, amount);
    }

    /// Owner-paid refill from the budget, for a device too empty to pay for
    /// its own draw. At most one float cap per call; does not count toward
    /// the period.
    public fun fund_device(
        budget: &mut FleetBudget,
        cap: &BudgetOwnerCap,
        registry: &DeviceRegistry,
        account: &mut GasAccount,
        amount: u64,
        ctx: &mut TxContext,
    ) {
        assert_owner(budget, cap);
        assert!(object::id(registry) == budget.registry_id, EWrongRegistry);
        assert_current(budget, account);
        assert!(amount > 0, EZeroAmount);
        assert!(amount <= account.limits.float_cap, EOverFloat);
        let device_id = account.device_id;
        let identity = device_registry::identity(registry, device_id);
        assert!(identity.is_some(), EInactiveDevice);
        let identity = identity.destroy_some();
        assert!(!device_registry::identity_revoked(&identity), EInactiveDevice);
        account.total_drawn = account.total_drawn + amount;
        pay(budget, device_id, device_registry::identity_signer(&identity), amount, ctx);
    }

    /// Owner changes to one account read the budget only to authorize.
    public fun set_device_limits(budget: &FleetBudget, cap: &BudgetOwnerCap, account: &mut GasAccount, limits: Limits) {
        assert_owner(budget, cap);
        assert_current(budget, account);
        assert_valid(&limits);
        account.limits = limits;
        emit_state(account, true);
    }

    /// Stop or resume one device's draws and uploads without touching its
    /// registry identity.
    public fun set_device_suspended(budget: &FleetBudget, cap: &BudgetOwnerCap, account: &mut GasAccount, suspended: bool) {
        assert_owner(budget, cap);
        assert_current(budget, account);
        account.suspended = suspended;
        emit_state(account, true);
    }

    /// Remove a device (for example after a handover to another owner): its
    /// account is closed for good and its reserve returns to the budget.
    public fun remove_device(budget: &mut FleetBudget, cap: &BudgetOwnerCap, account: &mut GasAccount) {
        assert!(budget.version == VERSION, EWrongVersion);
        remove_internal(budget, cap, account);
    }
    public(package) fun remove_managed(budget: &mut FleetBudget, cap: &BudgetOwnerCap, account: &mut GasAccount) {
        assert!(budget.version == MANAGED_VERSION, EWrongVersion);
        remove_internal(budget, cap, account);
    }
    fun remove_internal(budget: &mut FleetBudget, cap: &BudgetOwnerCap, account: &mut GasAccount) {
        assert_owner(budget, cap);
        assert_current(budget, account);
        let _: ID = df::remove(&mut budget.id, DeviceKey { device_id: account.device_id });
        budget.device_count = budget.device_count - 1;
        account.closed = true;
        account.suspended = true;
        let rest = account.reserve.value();
        if (rest > 0) reclaim(budget, account, rest);
        emit_state(account, false);
    }

    /// Move the budget to this package version. Older versions then refuse
    /// every mutating call on it and its accounts.
    public fun migrate(budget: &mut FleetBudget, cap: &BudgetOwnerCap) {
        assert!(object::id(cap) == budget.owner_cap_id && cap.budget_id == object::id(budget), ENotOwner);
        assert!(budget.version < VERSION, ENotUpgrade);
        budget.version = VERSION;
    }

    // ── Device ────────────────────────────────────────────────────────────────

    /// Top up the sending device's address by `amount` from its own account,
    /// within its limits. The budget is only read. The sender must be the
    /// device's current registry signer and hold its signer-bound DeviceCap.
    /// The coin goes to the sender inside the call, so a draw is exactly one
    /// command the signing service can check.
    #[allow(lint(self_transfer))]
    public fun draw_gas(
        budget: &FleetBudget,
        account: &mut GasAccount,
        registry: &DeviceRegistry,
        cap: &DeviceCap,
        amount: u64,
        clock: &Clock,
        ctx: &mut TxContext,
    ) {
        assert_may_draw(budget, account, registry, cap, ctx);
        assert!(amount > 0, EZeroAmount);
        let now = clock.timestamp_ms();
        roll_period(account, now);
        let limits = account.limits;
        assert!(amount <= limits.float_cap, EOverFloat);
        assert!(account.draws_in_period < limits.max_draws_per_period, ERateLimited);
        assert!(account.last_draw_ms == 0 || now >= account.last_draw_ms + limits.min_draw_interval_ms, ERateLimited);
        assert!(account.drawn_in_period + amount <= limits.period_allowance, EOverAllowance);
        assert!(account.reserve.value() >= amount, EInsufficientBalance);
        account.drawn_in_period = account.drawn_in_period + amount;
        account.draws_in_period = account.draws_in_period + 1;
        account.last_draw_ms = now;
        account.total_drawn = account.total_drawn + amount;
        let sender = ctx.sender();
        transfer::public_transfer(coin::take(&mut account.reserve, amount, ctx), sender);
        event::emit(GasDrawn {
            budget_id: account.budget_id,
            device_id: account.device_id,
            account_id: object::id(account),
            device_address: sender,
            amount,
            drawn_in_period: account.drawn_in_period,
            period_allowance: limits.period_allowance,
            draws_in_period: account.draws_in_period,
            max_draws_per_period: limits.max_draws_per_period,
            reserve: account.reserve.value(),
            at_limit: account.drawn_in_period >= limits.period_allowance || account.draws_in_period >= limits.max_draws_per_period,
        });
    }

    /// Abort unless the sender may currently draw or upload for `cap`'s
    /// device: current package, budget unpaused, this registry, the device's
    /// current, open and unsuspended account, the cap's signer and an active
    /// registry identity still bound to the enrolled owner cap. Reads only.
    /// Used by `fleet_storage`.
    public fun assert_may_draw(budget: &FleetBudget, account: &GasAccount, registry: &DeviceRegistry, cap: &DeviceCap, ctx: &TxContext) {
        assert!(budget.version == VERSION || (budget.version == MANAGED_VERSION && df::exists(&budget.id, CustomerManagedKey {})), EWrongVersion);
        assert!(!budget.paused, EPaused);
        assert!(object::id(registry) == budget.registry_id, EWrongRegistry);
        assert!(account.device_id == device_cap::device_id(cap), EWrongAccount);
        assert_current(budget, account);
        assert!(!account.suspended, ESuspended);
        device_cap::assert_active(cap, registry, ctx);
        let identity = device_registry::identity(registry, account.device_id).destroy_some();
        assert!(device_registry::identity_owner_cap(&identity) == account.owner_cap_id, EOwnerChanged);
    }

    /// Abort unless `cap` manages `budget` (for `fleet_storage`).
    public fun assert_owner(budget: &FleetBudget, cap: &BudgetOwnerCap) {
        assert!(budget.version == VERSION || (budget.version == MANAGED_VERSION && df::exists(&budget.id, CustomerManagedKey {})), EWrongVersion);
        assert!(object::id(cap) == budget.owner_cap_id && cap.budget_id == object::id(budget), ENotOwner);
    }

    /// Adoption seals out all older package versions before capability custody.
    public(package) fun adopt_managed(budget: &mut FleetBudget, cap: &BudgetOwnerCap) {
        assert_owner(budget, cap);
        budget.version = MANAGED_VERSION;
        df::add(&mut budget.id, CustomerManagedKey {}, true);
    }

    // ── Internal ──────────────────────────────────────────────────────────────

    fun assert_valid(limits: &Limits) {
        assert!(
            limits.period_ms > 0 && limits.max_draws_per_period > 0
                && limits.float_cap <= MAX_LIMIT_MIST && limits.period_allowance <= MAX_LIMIT_MIST,
            EInvalidLimits,
        );
    }

    /// The account belongs to this budget and is its device's current one.
    fun assert_current(budget: &FleetBudget, account: &GasAccount) {
        assert!(account.budget_id == object::id(budget), EWrongAccount);
        let key = DeviceKey { device_id: account.device_id };
        assert!(!account.closed && df::exists(&budget.id, key), ENotEnrolled);
        assert!(*df::borrow<DeviceKey, ID>(&budget.id, key) == object::id(account), EWrongAccount);
    }

    fun roll_period(account: &mut GasAccount, now: u64) {
        if (now >= account.period_start_ms + account.limits.period_ms) {
            account.period_start_ms = now;
            account.drawn_in_period = 0;
            account.draws_in_period = 0;
        };
    }

    fun allocate(budget: &mut FleetBudget, account: &mut GasAccount, amount: u64) {
        assert!(budget.balance.value() >= amount, EInsufficientBalance);
        account.reserve.join(budget.balance.split(amount));
        budget.total_allocated = budget.total_allocated + amount;
        emit_reserve(budget, account, true, amount);
        check_low(budget);
    }

    fun reclaim(budget: &mut FleetBudget, account: &mut GasAccount, amount: u64) {
        budget.balance.join(account.reserve.split(amount));
        budget.total_reclaimed = budget.total_reclaimed + amount;
        emit_reserve(budget, account, false, amount);
    }

    fun emit_reserve(budget: &FleetBudget, account: &GasAccount, allocated: bool, amount: u64) {
        event::emit(ReserveChanged {
            budget_id: object::id(budget),
            device_id: account.device_id,
            account_id: object::id(account),
            allocated,
            amount,
            reserve: account.reserve.value(),
            balance: budget.balance.value(),
        });
    }

    fun emit_state(account: &GasAccount, enrolled: bool) {
        event::emit(DeviceStateChanged {
            budget_id: account.budget_id,
            device_id: account.device_id,
            account_id: object::id(account),
            enrolled,
            suspended: account.suspended,
            limits: account.limits,
        });
    }

    fun pay(budget: &mut FleetBudget, device_id: ID, recipient: address, amount: u64, ctx: &mut TxContext) {
        assert!(budget.balance.value() >= amount, EInsufficientBalance);
        budget.total_paid = budget.total_paid + amount;
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

    // ── Reads (interface version 1) ───────────────────────────────────────────

    public fun interface_version(): u64 { INTERFACE_VERSION }
    public fun package_version(): u64 { VERSION }
    public fun version(budget: &FleetBudget): u64 { budget.version }
    public fun budget_id(cap: &BudgetOwnerCap): ID { cap.budget_id }
    public fun registry_id(budget: &FleetBudget): ID { budget.registry_id }
    public fun owner_cap_id(budget: &FleetBudget): ID { budget.owner_cap_id }
    /// Unallocated funds; device reserves are read from their accounts.
    public fun balance(budget: &FleetBudget): u64 { budget.balance.value() }
    /// Funds the owner may allocate. Equals `balance` until SF-092 adds reserves.
    public fun spendable(budget: &FleetBudget): u64 { budget.balance.value() }
    /// Funds held back for SF-092 renewals. Zero until SF-092 adds them.
    #[allow(lint(unused_object_with_fields))]
    public fun reserved(_budget: &FleetBudget): u64 { 0 }
    public fun paused(budget: &FleetBudget): bool { budget.paused }
    public fun default_limits(budget: &FleetBudget): Limits { budget.default_limits }
    public fun low_balance_threshold(budget: &FleetBudget): u64 { budget.low_balance_threshold }
    public fun device_count(budget: &FleetBudget): u64 { budget.device_count }
    public fun total_deposited(budget: &FleetBudget): u64 { budget.total_deposited }
    public fun total_withdrawn(budget: &FleetBudget): u64 { budget.total_withdrawn }
    public fun total_paid(budget: &FleetBudget): u64 { budget.total_paid }
    public fun total_allocated(budget: &FleetBudget): u64 { budget.total_allocated }
    public fun total_reclaimed(budget: &FleetBudget): u64 { budget.total_reclaimed }
    public fun is_enrolled(budget: &FleetBudget, device_id: ID): bool { df::exists(&budget.id, DeviceKey { device_id }) }
    /// The device's current account, if enrolled.
    public fun account_id(budget: &FleetBudget, device_id: ID): Option<ID> {
        if (!df::exists(&budget.id, DeviceKey { device_id })) return option::none();
        option::some(*df::borrow(&budget.id, DeviceKey { device_id }))
    }

    public fun limits_float_cap(limits: &Limits): u64 { limits.float_cap }
    public fun limits_period_ms(limits: &Limits): u64 { limits.period_ms }
    public fun limits_period_allowance(limits: &Limits): u64 { limits.period_allowance }
    public fun limits_max_draws_per_period(limits: &Limits): u64 { limits.max_draws_per_period }
    public fun limits_min_draw_interval_ms(limits: &Limits): u64 { limits.min_draw_interval_ms }

    public fun account_budget_id(account: &GasAccount): ID { account.budget_id }
    public fun account_device_id(account: &GasAccount): ID { account.device_id }
    public fun account_owner_cap_id(account: &GasAccount): ID { account.owner_cap_id }
    public fun account_reserve(account: &GasAccount): u64 { account.reserve.value() }
    public fun account_limits(account: &GasAccount): Limits { account.limits }
    public fun account_suspended(account: &GasAccount): bool { account.suspended }
    public fun account_closed(account: &GasAccount): bool { account.closed }
    public fun account_period_start_ms(account: &GasAccount): u64 { account.period_start_ms }
    public fun account_drawn_in_period(account: &GasAccount): u64 { account.drawn_in_period }
    public fun account_draws_in_period(account: &GasAccount): u64 { account.draws_in_period }
    public fun account_last_draw_ms(account: &GasAccount): u64 { account.last_draw_ms }
    public fun account_total_drawn(account: &GasAccount): u64 { account.total_drawn }
    public fun account_enrolled_at_ms(account: &GasAccount): u64 { account.enrolled_at_ms }

    /// Adoption keeps the account ID/reserve/limits but binds it to the new
    /// custodied capability; unrelated old accounts remain retired by identity.
    public(package) fun rebind_account(budget: &FleetBudget, account: &mut GasAccount, registry: &DeviceRegistry, cap: &DeviceOwnerCap) {
        assert_current(budget, account);
        assert!(object::id(registry) == budget.registry_id && account.device_id == device_registry::owner_cap_device_id(cap), EWrongAccount);
        let identity = device_registry::identity(registry, account.device_id).destroy_some();
        assert!(device_registry::identity_owner_cap(&identity) == object::id(cap), EOwnerChanged);
        account.owner_cap_id = object::id(cap);
    }

    /// For per-fleet records kept by other modules of this package
    /// (`fleet_settings`); each module owns its own dynamic-field key types.
    public(package) fun uid(budget: &FleetBudget): &UID { &budget.id }
    public(package) fun uid_mut(budget: &mut FleetBudget): &mut UID { &mut budget.id }

    #[test_only]
    public fun set_version_for_testing(budget: &mut FleetBudget, version: u64) { budget.version = version; }
}
