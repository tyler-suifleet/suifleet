/// Shared second-factor enforcement. Only reviewed in-package mechanism modules
/// may attest; a public caller cannot manufacture a factor or a transaction grant.
module sui_edge::security_policy {
    const SMALL_604800000: u32 = 604800000;
    const SMALL_4096: u16 = 4096;

    // Widen named narrow literals at use sites to avoid repeated LdU64.
    // The pinned compiler uses LdU8/U16/U32 followed by CastU64. Values and u64 types
    // are unchanged; abort constants retain their original u64 declarations.
    const SMALL_0: u8 = 0;
    const SMALL_1: u8 = 1;
    const SMALL_4: u8 = 4;
    const SMALL_7: u8 = 7;
    const SMALL_32: u8 = 32;

    use std::bcs;
    use sui::dynamic_field as df;
    use sui::event;
    use sui::clock::Clock;
    use sui::hash;

    const EPolicy: u64 = 0;
    const EFactor: u64 = 1;
    const EUnavailable: u64 = 2;
    const ACTIONS: u64 = 7;
    // Native multisig and delay are implemented; other factors remain fail-closed.
    const AVAILABLE: u8 = 9;

    // Separate fields preserve the published Policy and Action layouts/origins.
    public struct DelayKey has copy, drop, store {}
    public struct Delays has copy, drop, store { milliseconds: vector<u64> }
    public struct QueuedAtKey has copy, drop, store {}
    public struct QueuedAt has copy, drop, store { milliseconds: u64 }
    public struct DelayGrantKey has copy, drop, store {}
    public struct DelayGrant has copy, drop, store { elapsed_ms: u64, transaction: vector<u8>, pause: bool }
    const EDelay: u64 = 3;
    /// One second through six days; expiry is independently bounded to seven days.
    public(package) fun configure_delays(uid: &mut UID, milliseconds: vector<u64>, ctx: &TxContext) {
        assert!(milliseconds.length() == ACTIONS, EPolicy);
        let mut i = 0;
        while (i < ACTIONS) {
            assert!(milliseconds[i] >= 1000 && milliseconds[i] <= 518400000, EDelay);
            i = i + 1;
        };
        configure_factor(uid, 8, vector[1], ctx);
        if (df::exists(uid, DelayKey {})) {
            df::borrow_mut<DelayKey, Delays>(uid, DelayKey {}).milliseconds = milliseconds;
        } else df::add(uid, DelayKey {}, Delays { milliseconds });
    }
    public(package) fun delays(uid: &UID): vector<u64> {
        if (df::exists(uid, DelayKey {})) df::borrow<DelayKey, Delays>(uid, DelayKey {}).milliseconds
        else vector[]
    }
    public(package) fun queued_at(a: &Action): u64 {
        // Legacy queue entries have no trustworthy timestamp and fail closed.
        assert!(df::exists(&a.id, QueuedAtKey {}), EDelay);
        df::borrow<QueuedAtKey, QueuedAt>(&a.id, QueuedAtKey {}).milliseconds
    }
    fun remove_timestamp(uid: &mut UID) {
        if (df::exists(uid, ArgumentsKey {})) { df::remove<ArgumentsKey, ActionArguments>(uid, ArgumentsKey {}); };
        if (df::exists(uid, QueuedAtKey {})) { df::remove<QueuedAtKey, QueuedAt>(uid, QueuedAtKey {}); };
    }
    public struct PolicyKey has copy, drop, store {}
    public struct Policy has copy, drop, store {
        schema: u64,
        generation: u64,
        // Publish, deploy/advance, access, withdrawals, credentials/policy,
        // handover, other administration. Bits: multisig/passkey/person/delay.
        requirements: vector<u8>,
        transaction: vector<u8>,
        attested: u8,
    }
    public struct FactorKey has copy, drop, store { factor: u8 }
    public struct FactorConfiguration has copy, drop, store { binding: vector<u8> }
    /// Only the future authenticated in-package verifier may enroll a source.
    /// Changing/replacing one uses every currently enforced class, never the
    /// proposed new source, and invalidates outstanding proofs.
    public(package) fun configure_factor(uid: &mut UID, factor: u8, binding: vector<u8>, ctx: &TxContext) {
        if (!(factor == 1)) { if (!(factor == 2)) { if (!(factor == 4)) { p0(factor == 8); }; }; };
        check_all(uid, ctx);
        p1(!binding.is_empty()); p1(binding.length() <= (SMALL_4096 as u64));
        let key = FactorKey { factor };
        if (df::exists(uid, key)) {
            *df::borrow_mut<FactorKey, FactorConfiguration>(uid, key) = FactorConfiguration { binding };
        } else df::add(uid, key, FactorConfiguration { binding });
        invalidate(uid);
    }
    public(package) fun factor_configuration(uid: &UID, factor: u8): vector<u8> {
        let key = FactorKey { factor };
        if (df::exists(uid, key)) df::borrow<FactorKey, FactorConfiguration>(uid, key).binding else vector[]
    }
    public struct PolicyChanged has copy, drop {
        customer_id: ID, generation: u64, requirements: vector<u8>,
    }

    public fun available(): u8 { AVAILABLE }
    public fun action_count(): u64 { ACTIONS }
    public(package) fun initialize(uid: &mut UID) {
        p1(!df::exists(uid, PolicyKey {}));
        df::add(uid, PolicyKey {}, Policy { schema: (SMALL_1 as u64), generation: (SMALL_0 as u64),
            requirements: vector[0, 0, 0, 0, 0, 0, 0], transaction: vector[], attested: 0 });
    }
    public(package) fun check(uid: &UID, action: u64, ctx: &TxContext) {
        let p = df::borrow<PolicyKey, Policy>(uid, PolicyKey {});
        p1(p.schema == (SMALL_1 as u64)); p1(action < ACTIONS);
        let needed = p.requirements[action];
        if (!(needed == 0)) { p0(p.transaction == *ctx.digest()); p0((p.attested & needed) == needed); };
        if ((needed & 8) != 0) {
            assert!(df::exists(uid, DelayGrantKey {}), EDelay);
            let grant = df::borrow<DelayGrantKey, DelayGrant>(uid, DelayGrantKey {});
            let durations = delays(uid);
            assert!(grant.transaction == *ctx.digest() && durations.length() == ACTIONS
                && ((grant.pause && action == 1) || grant.elapsed_ms >= durations[action]), EDelay);
        };
    }
    /// Capability lending cannot be authorized by a wrapper grant: a caller
    /// could otherwise substitute any operation while holding the capability.
    public(package) fun assert_unprotected(uid: &UID) {
        let p = read(uid);
        let mut i = (SMALL_0 as u64);
        while (i < ACTIONS) { p0(p.requirements[i] == 0); i = i + (SMALL_1 as u64); };
    }
    public(package) fun check_all(uid: &UID, ctx: &TxContext) {
        let mut i = (SMALL_0 as u64);
        while (i < ACTIONS) { check(uid, i, ctx); i = i + (SMALL_1 as u64); };
    }
    public(package) fun update(uid: &mut UID, requirements: vector<u8>, ctx: &TxContext) {
        assert!(requirements.length() == ACTIONS, EPolicy);
        let current = read(uid);
        check(uid, (SMALL_4 as u64), ctx);
        let mut j = (SMALL_0 as u64);
        while (j < ACTIONS) {
            if ((current.requirements[j] & requirements[j]) != current.requirements[j]) check(uid, j, ctx);
            j = j + (SMALL_1 as u64);
        };
        let mut i = (SMALL_0 as u64);
        while (i < ACTIONS) {
            assert!((requirements[i] & 15) == requirements[i], EPolicy);
            assert!((requirements[i] & AVAILABLE) == requirements[i], EUnavailable);
            let mut bit = 1;
            while (bit <= 8) {
                if ((requirements[i] & bit) != 0) assert!(!factor_configuration(uid, bit).is_empty(), EFactor);
                bit = bit * 2;
            };
            i = i + (SMALL_1 as u64);
        };
        let customer_id = uid.to_inner();
        let p = df::borrow_mut<PolicyKey, Policy>(uid, PolicyKey {});
        p.requirements = requirements;
        p.generation = p.generation + (SMALL_1 as u64);
        p.transaction = vector[];
        p.attested = 0;
        event::emit(PolicyChanged { customer_id, generation: p.generation, requirements });
    }
    /// Credential/configuration mutation invalidates every queued authorization.
    public(package) fun invalidate(uid: &mut UID) {
        if (df::exists(uid, DelayGrantKey {})) { df::remove<DelayGrantKey, DelayGrant>(uid, DelayGrantKey {}); };
        let p = df::borrow_mut<PolicyKey, Policy>(uid, PolicyKey {});
        p.generation = p.generation + (SMALL_1 as u64);
        p.transaction = vector[];
        p.attested = 0;
    }
    /// Newly introduced type: old packages cannot create, mutate or consume it.
    /// No store or public transfer: an action remains shared until execution or
    /// cancellation; child modules verify proofs against its immutable intent.
    public struct Action has key {
        id: UID, customer_id: ID, generation: u64, proposer: address,
        intent: vector<u8>, expires_ms: u64, attested: u8,
    }
    public struct ActionCreated has copy, drop {
        action_id: ID, customer_id: ID, generation: u64, proposer: address,
        intent: vector<u8>, expires_ms: u64,
    }
    public struct ArgumentsKey has copy, drop, store {}
    public struct ActionArguments has copy, drop, store { bytes: vector<u8> }
    public(package) fun prepare_with_arguments(uid: &UID, bytes: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        assert!(bytes.length() <= 65536, EPolicy);
        let intent = action_intent(uid.to_inner(), bytes);
        let mut a = new_action(uid, intent, expires_ms, clock, ctx);
        df::add(&mut a.id, ArgumentsKey {}, ActionArguments { bytes });
        transfer::share_object(a);
    }
    public(package) fun prepare(uid: &UID, intent: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        transfer::share_object(new_action(uid, intent, expires_ms, clock, ctx));
    }
    fun new_action(uid: &UID, intent: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext): Action {
        p1(intent.length() == (SMALL_32 as u64));
        let now = clock.timestamp_ms();
        p1(expires_ms > now); p1(expires_ms - now <= (SMALL_604800000 as u64));
        let p = read(uid);
        let mut a = Action { id: object::new(ctx), customer_id: uid.to_inner(), generation: p.generation,
            proposer: ctx.sender(), intent, expires_ms, attested: 0 };
        df::add(&mut a.id, QueuedAtKey {}, QueuedAt { milliseconds: now });
        event::emit(ActionCreated { action_id: object::id(&a), customer_id: a.customer_id,
            generation: a.generation, proposer: a.proposer, intent, expires_ms });
        a
    }
    /// Domain: introduction-package identity plus the globally unique customer
    /// object ID. Object IDs derive from chain transaction digests; neither a UI
    /// label nor a caller-supplied network string defines the signing domain.
    public fun action_intent(customer: ID, arguments_bcs: vector<u8>): vector<u8> {
        hash::blake2b256(&bcs::to_bytes(&vector[
            bcs::to_bytes(&std::type_name::with_original_ids<Action>()),
            bcs::to_bytes(&customer), arguments_bcs,
        ]))
    }
    public fun intent(a: &Action): vector<u8> { a.intent }
    public fun customer_id(a: &Action): ID { a.customer_id }
    public fun proposer(a: &Action): address { a.proposer }
    public fun action_generation(a: &Action): u64 { a.generation }
    public fun expires_ms(a: &Action): u64 { a.expires_ms }
    /// A child verifier checks its configured cryptographic/approval/timing
    /// evidence, current authority, intent and expiry before marking its bit.
    /// This function is package-only; no public generic witness is accepted.
    public(package) fun attest(a: &mut Action, factor: u8) {
        if (!(factor == 1)) { if (!(factor == 2)) { if (!(factor == 4)) { p0(factor == 8); }; }; };
        a.attested = a.attested | factor;
    }
    /// Each protected wrapper computes its own canonical intent, consumes the
    /// action, performs one operation, then clears this internal stack-scoped
    /// grant before returning to the PTB. No public begin/end entry points.
    public(package) fun begin(uid: &mut UID, a: Action, arguments: vector<vector<u8>>, clock: &Clock, ctx: &TxContext) {
        begin_scoped(uid, a, arguments, (SMALL_7 as u64), false, clock, ctx);
    }
    /// Class 7 is reserved for changes that require the union of current policy.
    /// Pause skips only delay; it cannot alter schedule, recipients or targets.
    public(package) fun begin_scoped(uid: &mut UID, a: Action, arguments: vector<vector<u8>>, class: u64, pause: bool, clock: &Clock, ctx: &TxContext) {
        begin_scoped_bytes(uid, a, bcs::to_bytes(&arguments), class, pause, clock, ctx);
    }
    public(package) fun begin_scoped_bytes(uid: &mut UID, a: Action, arguments_bcs: vector<u8>, class: u64, pause: bool, clock: &Clock, ctx: &TxContext) {
        assert!(class <= ACTIONS && (!pause || class == (SMALL_1 as u64)), EPolicy);
        let mut a = a;
        let current = read(uid);
        let mut delay = 0;
        let mut required = 0;
        let durations = delays(uid);
        let mut j = 0;
        while (j < ACTIONS) {
            if (class == ACTIONS || class == j) {
                required = required | current.requirements[j];
                if (!pause && (current.requirements[j] & 8) != 0) {
                    assert!(durations.length() == ACTIONS, EDelay);
                    if (durations[j] > delay) delay = durations[j];
                };
            };
            j = j + 1;
        };
        let elapsed_ms = if (df::exists(&a.id, QueuedAtKey {})) {
            let created = queued_at(&a);
            assert!(clock.timestamp_ms() >= created, EDelay);
            clock.timestamp_ms() - created
        } else { assert!(delay == 0, EDelay); 0 };
        assert!(elapsed_ms >= delay, EDelay);
        if (df::exists(uid, DelayGrantKey {})) { df::remove<DelayGrantKey, DelayGrant>(uid, DelayGrantKey {}); };
        df::add(uid, DelayGrantKey {}, DelayGrant { elapsed_ms, transaction: *ctx.digest(), pause });
        remove_timestamp(&mut a.id);
        let Action { id, customer_id, generation, proposer, intent, expires_ms, attested } = a;
        // Timing is rechecked at consumption, never a persistent attestation.
        let attested = attested | 8;
        assert!(customer_id == uid.to_inner() && proposer == ctx.sender(), EPolicy);
        assert!(clock.timestamp_ms() < expires_ms, EPolicy);
        assert!(intent == action_intent(uid.to_inner(), arguments_bcs), EPolicy);
        let p = df::borrow_mut<PolicyKey, Policy>(uid, PolicyKey {});
        assert!(p.schema == (SMALL_1 as u64) && p.generation == generation, EPolicy);
        if (pause) required = required & 7;
        assert!((attested & required) == required, EFactor);
        p.transaction = *ctx.digest();
        p.attested = attested;
        id.delete();
    }
    public(package) fun finish(uid: &mut UID) { invalidate(uid); }
    public(package) fun cancel(uid: &UID, a: Action) {
        let mut a = a;
        remove_timestamp(&mut a.id);
        let Action { id, customer_id, generation: _, proposer: _, intent: _, expires_ms: _, attested: _ } = a;
        p1(customer_id == uid.to_inner());
        id.delete();
    }
    public(package) fun read(uid: &UID): Policy { *df::borrow<PolicyKey, Policy>(uid, PolicyKey {}) }
    public fun generation(p: &Policy): u64 { p.generation }
    public fun requirements(p: &Policy): vector<u8> { p.requirements }

    #[test_only]
    public fun remove_for_testing(uid: &mut UID) { df::remove<PolicyKey, Policy>(uid, PolicyKey {}); }

    #[test_only]
    public fun set_for_testing(uid: &mut UID, requirements: vector<u8>) {
        p1(requirements.length() == ACTIONS);
        df::borrow_mut<PolicyKey, Policy>(uid, PolicyKey {}).requirements = requirements;
    }
    // Separate guard calls retain short-circuit order and the same abort site/code.
    // Avoid materializing intermediate booleans in the caller bytecode.
    // Share each named abort branch without changing its module or error code.
    /// check_Factor: compact private symbol to bound published bytecode storage.
    fun p0(condition: bool) {
        if (!condition) abort EFactor
    }
    /// check_Policy: compact private symbol to bound published bytecode storage.
    fun p1(condition: bool) {
        if (!condition) abort EPolicy
    }
}
