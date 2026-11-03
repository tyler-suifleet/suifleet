/// Claim enrollment (SF-021): devices enroll themselves into an operator's
/// fleet, in bulk, without SUI of their own.
///
/// An operator's `EnrollmentPolicy` holds single-use claim addresses: the
/// addresses of claim keys the operator generated and placed on devices or
/// their provisioning media. A device enrolls in one sponsored transaction:
///
/// - the **sender** is the device's own key, which becomes its trusted
///   identity: the transaction signature proves the device holds it;
/// - the **gas sponsor** is an unused claim address: the sponsor signature
///   proves the device holds that claim key, and the claim pays the gas.
///
/// The claim is consumed; the Device is registered and taken into registry
/// custody; the DeviceOwnerCap goes to the operator and a signer-bound
/// DeviceCap to the device. Without the claim key nobody can use the claim,
/// so it cannot be front-run.
///
/// Introduced by a package upgrade: older package versions cannot reach it.
module sui_edge::enrollment {
    // Named u64 constants share the bytecode constant pool, including error values,
    // and avoid repeated immediate loads or casts without changing numeric types.
    const SMALL_0: u64 = 0;
    const SMALL_1: u64 = 1;

    use std::string::String;
    use sui::clock::Clock;
    use sui::event;
    use sui::dynamic_field as df;
    use sui::table::{Self, Table};
    use sui_edge::device_cap;
    use sui_edge::device_registry::{Self, DeviceRegistry, DeviceOwnerCap};

    // ── Errors ────────────────────────────────────────────────────────────────

    const ENotOperator: u64 = 0;
    const EClosed: u64 = 1;
    const EExpired: u64 = 2;
    const EQuotaExhausted: u64 = 3;
    const ENotSponsored: u64 = 4;
    const EUnknownClaim: u64 = 5;
    const EWrongRegistry: u64 = 6;
    const ESponsorIsDevice: u64 = 7;

    // ── State ─────────────────────────────────────────────────────────────────

    public struct EnrollmentPolicy has key {
        id: UID,
        operator: address,
        registry: ID,
        /// Unused claim addresses.
        claims: Table<address, bool>,
        /// Enrollments still allowed.
        remaining: u64,
        /// Clock time after which enrollment is refused; 0 = no expiry.
        expires_at_ms: u64,
        open: bool,
        enrolled: u64,
    }

    public struct SecurityRevisionKey has copy, drop, store {}
    public struct SecurityGenerationKey has copy, drop, store {}
    // Existing key, distinct parent UIDs: Customer stores the authority epoch;
    // EnrollmentPolicy stores the epoch at which its claims were authorized.
    public(package) fun authority_generation(customer: &UID): u64 {
        if (df::exists(customer, SecurityGenerationKey {})) *df::borrow<SecurityGenerationKey, u64>(customer, SecurityGenerationKey {}) else SMALL_0
    }
    public(package) fun invalidate_authority(customer: &mut UID) {
        let next = authority_generation(customer) + SMALL_1;
        if (df::exists(customer, SecurityGenerationKey {})) *df::borrow_mut<SecurityGenerationKey, u64>(customer, SecurityGenerationKey {}) = next
        else df::add(customer, SecurityGenerationKey {}, next);
    }
    public(package) fun security_revision(policy: &EnrollmentPolicy): u64 {
        if (df::exists(&policy.id, SecurityRevisionKey {})) *df::borrow<SecurityRevisionKey, u64>(&policy.id, SecurityRevisionKey {}) else SMALL_0
    }
    /// changed: compact private symbol to bound published bytecode storage.
    fun p0(policy: &mut EnrollmentPolicy) {
        if (!df::exists(&policy.id, SecurityRevisionKey {})) df::add(&mut policy.id, SecurityRevisionKey {}, SMALL_0);
        let revision = df::borrow_mut<SecurityRevisionKey, u64>(&mut policy.id, SecurityRevisionKey {});
        *revision = *revision + SMALL_1;
    }
    public(package) fun bind_security_generation(policy: &mut EnrollmentPolicy, generation: u64) {
        if (df::exists(&policy.id, SecurityGenerationKey {})) {
            *df::borrow_mut<SecurityGenerationKey, u64>(&mut policy.id, SecurityGenerationKey {}) = generation;
        } else df::add(&mut policy.id, SecurityGenerationKey {}, generation);
    }
    public(package) fun security_generation(policy: &EnrollmentPolicy): u64 {
        if (df::exists(&policy.id, SecurityGenerationKey {})) *df::borrow<SecurityGenerationKey, u64>(&policy.id, SecurityGenerationKey {}) else SMALL_0
    }
    public struct ManagedKey has copy, drop, store {}
    public struct PolicyCreated has copy, drop {
        policy_id: ID,
        operator: address,
        registry: ID,
    }

    public struct DeviceEnrolled has copy, drop {
        policy_id: ID,
        device_id: ID,
        device_address: address,
        claim: address,
        operator: address,
        owner_cap: ID,
        device_cap: ID,
    }

    // ── Operator ──────────────────────────────────────────────────────────────

    /// A shared policy owned by the sender, for one registry.
    public fun create_policy(registry: &DeviceRegistry, quota: u64, expires_at_ms: u64, ctx: &mut TxContext) {
        let policy = EnrollmentPolicy {
            id: object::new(ctx),
            operator: ctx.sender(),
            registry: object::id(registry),
            claims: table::new(ctx),
            remaining: quota,
            expires_at_ms,
            open: true,
            enrolled: SMALL_0,
        };
        event::emit(PolicyCreated { policy_id: object::id(&policy), operator: policy.operator, registry: policy.registry });
        transfer::share_object(policy);
    }

    public(package) fun create_managed(registry: &DeviceRegistry, operator: address, quota: u64, expires_at_ms: u64, ctx: &mut TxContext): ID {
        let mut policy = EnrollmentPolicy { id: object::new(ctx), operator, registry: object::id(registry),
            claims: table::new(ctx), remaining: quota, expires_at_ms, open: false, enrolled: SMALL_0 };
        df::add(&mut policy.id, ManagedKey {}, true);
        let policy_id = object::id(&policy);
        event::emit(PolicyCreated { policy_id, operator, registry: policy.registry });
        transfer::share_object(policy); policy_id
    }
    public(package) fun add_claims_as(policy: &mut EnrollmentPolicy, claims: vector<address>, authority: address) {
        g0(policy.operator == authority, false);
        p0(policy);
        let mut i = SMALL_0;
        while (i < claims.length()) {
            if (!policy.claims.contains(claims[i])) policy.claims.add(claims[i], true);
            i = i + SMALL_1;
        };
    }
    public(package) fun revoke_claim_as(policy: &mut EnrollmentPolicy, claim: address, authority: address) {
        g0(policy.operator == authority, false);
        p0(policy);
        policy.claims.remove(claim);
    }
    public(package) fun configure_managed(policy: &mut EnrollmentPolicy, quota: u64, expires_at_ms: u64, close: bool, authority: address) {
        g0(policy.operator == authority, false);
        p0(policy);
        g0(df::exists(&policy.id, ManagedKey {}), true);
        policy.remaining = quota; policy.expires_at_ms = expires_at_ms;
        if (close) *df::borrow_mut<ManagedKey, bool>(&mut policy.id, ManagedKey {}) = false;
    }

    /// Add claim addresses (duplicates are ignored).
    #[allow(lint(prefer_mut_tx_context))]
    public fun add_claims(policy: &mut EnrollmentPolicy, claims: vector<address>, ctx: &TxContext) {
        p1(policy, ctx);
        let mut i = SMALL_0;
        while (i < claims.length()) {
            if (!policy.claims.contains(claims[i])) {
                policy.claims.add(claims[i], true);
            };
            i = i + SMALL_1;
        };
    }

    /// Withdraw an unused claim, e.g. for a lost or leaked claim key.
    #[allow(lint(prefer_mut_tx_context))]
    public fun revoke_claim(policy: &mut EnrollmentPolicy, claim: address, ctx: &TxContext) {
        p1(policy, ctx);
        g1(policy.claims.contains(claim), true);
        policy.claims.remove(claim);
    }

    #[allow(lint(prefer_mut_tx_context))]
    public fun set_limits(policy: &mut EnrollmentPolicy, quota: u64, expires_at_ms: u64, ctx: &TxContext) {
        p1(policy, ctx);
        policy.remaining = quota;
        policy.expires_at_ms = expires_at_ms;
    }

    /// Stop all enrollment under this policy (permanent).
    #[allow(lint(prefer_mut_tx_context))]
    public fun close(policy: &mut EnrollmentPolicy, ctx: &TxContext) {
        p1(policy, ctx);
        policy.open = false;
    }

    // ── Device ────────────────────────────────────────────────────────────────

    /// Enroll the sender's key as a new device of the policy's operator.
    /// Must be sponsored by an unused claim address of this policy.
    #[allow(lint(self_transfer))]
    public fun enroll(
        policy: &mut EnrollmentPolicy,
        registry: &mut DeviceRegistry,
        name: String,
        arch: String,
        clock: &Clock,
        ctx: &mut TxContext,
    ) {
        let cap = enroll_internal(policy, registry, name, arch, clock, ctx, false);
        transfer::public_transfer(cap, policy.operator);
    }
    public(package) fun enroll_managed(policy: &mut EnrollmentPolicy, registry: &mut DeviceRegistry,
        name: String, arch: String, clock: &Clock, ctx: &mut TxContext): DeviceOwnerCap {
        enroll_internal(policy, registry, name, arch, clock, ctx, true)
    }
    /// Private helper: enroll_internal.
    fun enroll_internal(policy: &mut EnrollmentPolicy, registry: &mut DeviceRegistry,
        name: String, arch: String, clock: &Clock, ctx: &mut TxContext, managed: bool): DeviceOwnerCap {
        g1(object::id(registry) == policy.registry, false);
        if (managed) {
            g0(!policy.open, true); g0(df::exists(&policy.id, ManagedKey {}), true); g0(*df::borrow<ManagedKey, bool>(&policy.id, ManagedKey {}), true);
        } else g0(policy.open, true);
        g2(policy.expires_at_ms == SMALL_0 || clock.timestamp_ms() <= policy.expires_at_ms, true);
        g2(policy.remaining > SMALL_0, false);
        let sponsor = ctx.sponsor();
        g3(sponsor.is_some(), true);
        let claim = sponsor.destroy_some();
        let device_address = ctx.sender();
        g3(claim != device_address, false);
        g1(policy.claims.contains(claim), true);
        policy.claims.remove(claim);
        policy.remaining = policy.remaining - SMALL_1;
        policy.enrolled = policy.enrolled + SMALL_1;

        let device = device_registry::register_device(registry, name, device_address, arch, clock, ctx);
        let device_id = device_registry::device_id(&device);
        let cap = device_cap::issue(&device, ctx);
        let owner_cap = device_registry::activate_device(registry, device, ctx);
        event::emit(DeviceEnrolled {
            policy_id: object::id(policy), device_id, device_address, claim, operator: policy.operator,
            owner_cap: object::id(&owner_cap), device_cap: object::id(&cap),
        });
        transfer::public_transfer(cap, device_address);
        owner_cap
    }

    // ── Reads ─────────────────────────────────────────────────────────────────

    public fun operator(policy: &EnrollmentPolicy): address { policy.operator }
    public fun is_claim(policy: &EnrollmentPolicy, claim: address): bool { policy.claims.contains(claim) }
    public fun remaining(policy: &EnrollmentPolicy): u64 { policy.remaining }
    public fun enrolled(policy: &EnrollmentPolicy): u64 { policy.enrolled }
    public fun is_open(policy: &EnrollmentPolicy): bool { policy.open }

    /// assert_operator: compact private symbol to bound published bytecode storage.
    fun p1(policy: &EnrollmentPolicy, ctx: &TxContext) {
        g0(ctx.sender() == policy.operator, false);
    }
    // Separate guard calls retain short-circuit order and the same abort site/code.
    // Avoid materializing intermediate booleans in the caller bytecode.
    // Share each named abort branch without changing its module or error code.

    // Private named guards retain module-local error codes; selectors are fixed at each call.
    fun g0(condition: bool, first: bool) { if (!condition) { if (first) abort EClosed; abort ENotOperator } }
    fun g1(condition: bool, first: bool) { if (!condition) { if (first) abort EUnknownClaim; abort EWrongRegistry } }
    fun g2(condition: bool, first: bool) { if (!condition) { if (first) abort EExpired; abort EQuotaExhausted } }
    fun g3(condition: bool, first: bool) { if (!condition) { if (first) abort ENotSponsored; abort ESponsorIsDevice } }
}
