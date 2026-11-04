/// Shared second-factor enforcement. Only reviewed in-package mechanism modules
/// may attest; a public caller cannot manufacture a factor or a transaction grant.
module sui_edge::security_policy {
    const SMALL_604800000: u64 = 604800000;
    const SMALL_4096: u64 = 4096;

    // Named u64 constants share the bytecode constant pool, including error values,
    // and avoid repeated immediate loads or casts without changing numeric types.
    const SMALL_0: u64 = 0;
    const SMALL_1: u64 = 1;
    const SMALL_2: u64 = 2;
    const SMALL_4: u64 = 4;
    const SMALL_7: u64 = 7;
    const SMALL_32: u64 = 32;
    const SMALL_1000: u64 = 1000;
    const SMALL_518400000: u64 = 518400000;

    use std::bcs;
    use sui::dynamic_field as df;
    use sui::event;
    use sui::clock::Clock;
    use sui::hash;

    const EPolicy: u64 = 0;
    const EFactor: u64 = 1;
    const EUnavailable: u64 = 2;
    const ACTIONS: u64 = 7;
    // Native multisig, passkey, second-person and delay are available.
    const AVAILABLE: u8 = 15;

    // Separate fields preserve the published Policy and Action layouts/origins.
    public struct DelayKey has copy, drop, store {}
    public struct Delays has copy, drop, store { milliseconds: vector<u64> }
    public struct QueuedAtKey has copy, drop, store {}
    public struct QueuedAt has copy, drop, store { milliseconds: u64 }
    public struct DelayGrantKey has copy, drop, store {}
    public struct DelayGrant has copy, drop, store { elapsed_ms: u64, transaction: vector<u8>, pause: bool }
    const EDelay: u64 = 3;
    /// One second through six days; expiry is independently bounded to seven days.
    /// Package helper: configure_delays.
    public(package) fun k0(uid: &mut UID, milliseconds: vector<u64>, ctx: &TxContext) {
        z0(milliseconds.length() == ACTIONS);
        let mut i = SMALL_0;
        while (i < ACTIONS) {
            z1(milliseconds[i] >= SMALL_1000); z1(milliseconds[i] <= SMALL_518400000);
            i = i + SMALL_1;
        };
        k3(uid, 8, vector[1], ctx);
        if (df::exists(uid, DelayKey {})) {
            df::borrow_mut<DelayKey, Delays>(uid, DelayKey {}).milliseconds = milliseconds;
        } else df::add(uid, DelayKey {}, Delays { milliseconds });
    }
    /// Package helper: delays.
    public(package) fun k1(uid: &UID): vector<u64> {
        if (df::exists(uid, DelayKey {})) df::borrow<DelayKey, Delays>(uid, DelayKey {}).milliseconds
        else v0()
    }
    /// Package helper: queued_at.
    public(package) fun k2(a: &Action): u64 {
        // Legacy queue entries have no trustworthy timestamp and fail closed.
        z1(df::exists(&a.id, QueuedAtKey {}));
        df::borrow<QueuedAtKey, QueuedAt>(&a.id, QueuedAtKey {}).milliseconds
    }
    /// Private helper: remove_timestamp.
    fun h0(uid: &mut UID) {
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
    /// Package helper: configure_factor.
    public(package) fun k3(uid: &mut UID, factor: u8, binding: vector<u8>, ctx: &TxContext) {
        if (!(factor == 1)) { if (!(factor == 2)) { if (!(factor == 4)) { z2(factor == 8); }; }; };
        k9(uid, ctx);
        z0(binding.length() <= SMALL_4096);
        let key = FactorKey { factor };
        if (binding.is_empty()) {
            if (df::exists(uid, key)) { df::remove<FactorKey, FactorConfiguration>(uid, key); };
        } else if (df::exists(uid, key)) {
            *df::borrow_mut<FactorKey, FactorConfiguration>(uid, key) = FactorConfiguration { binding };
        } else df::add(uid, key, FactorConfiguration { binding });
        k11(uid);
    }
    /// Package helper: factor_configuration.
    public(package) fun k4(uid: &UID, factor: u8): vector<u8> {
        let key = FactorKey { factor };
        if (df::exists(uid, key)) df::borrow<FactorKey, FactorConfiguration>(uid, key).binding else v0()
    }
    public struct PolicyChanged has copy, drop {
        customer_id: ID, generation: u64, requirements: vector<u8>,
    }

    public fun available(): u8 { AVAILABLE }
    public fun action_count(): u64 { ACTIONS }
    /// Package helper: initialize.
    public(package) fun k5(uid: &mut UID) {
        z0(!df::exists(uid, PolicyKey {}));
        df::add(uid, PolicyKey {}, Policy { schema: SMALL_1, generation: SMALL_0,
            requirements: vector[0, 0, 0, 0, 0, 0, 0], transaction: v0(), attested: 0 });
    }
    /// Package helper: check.
    public(package) fun k6(uid: &UID, action: u64, ctx: &TxContext) {
        let p = df::borrow<PolicyKey, Policy>(uid, PolicyKey {});
        z0(p.schema == SMALL_1); z0(action < ACTIONS);
        let needed = p.requirements[action];
        let authenticated = h5(uid, ctx);
        assert!((authenticated & needed) == needed || (p.transaction == *ctx.digest() && ((p.attested | authenticated) & needed) == needed), EFactor);
        if ((needed & 8) != 0) {
            z1(df::exists(uid, DelayGrantKey {}));
            let grant = df::borrow<DelayGrantKey, DelayGrant>(uid, DelayGrantKey {});
            let durations = k1(uid);
            z1(grant.transaction == *ctx.digest()); z1(durations.length() == ACTIONS); z1(((grant.pause && action == SMALL_1) || grant.elapsed_ms >= durations[action]));
        };
    }
    /// Capability lending cannot be authorized by a wrapper grant: a caller
    /// could otherwise substitute any operation while holding the capability.
    /// Package helper: assert_unprotected.
    public(package) fun k7(uid: &UID) {
        let p = k23(uid);
        let mut i = SMALL_0;
        while (i < ACTIONS) { z2(p.requirements[i] == 0); i = i + SMALL_1; };
    }
    /// Validator-verified same-digest sponsorship proves the enrolled passkey.
    /// Private helper: passkey_attestation.
    fun h5(uid: &UID, ctx: &TxContext): u8 {
        let sponsor = ctx.sponsor();
        if (sponsor.is_none() || *sponsor.borrow() == ctx.sender()) return 0;
        let binding = k4(uid, 2);
        if (binding.is_empty()) return 0;
        let mut reader = sui::bcs::new(binding);
        let entries = reader.peel_vec_address();
        z0(reader.into_remainder_bytes().is_empty()); z0(entries.length() % SMALL_2 == 0);
        let mut i = SMALL_0;
        while (i < entries.length()) {
            if (entries[i] == ctx.sender() && entries[i + 1] == *sponsor.borrow()) return 2;
            i = i + SMALL_2;
        };
        0
    }
    /// Package helper: check_capability_loan.
    public(package) fun k8(uid: &UID, ctx: &TxContext) {
        let p = k23(uid);
        let authenticated = h5(uid, ctx);
        let mut i = SMALL_0;
        while (i < ACTIONS) {
            z2((p.requirements[i] & authenticated) == p.requirements[i]);
            i = i + SMALL_1;
        };
    }
    /// Package helper: check_all.
    public(package) fun k9(uid: &UID, ctx: &TxContext) {
        let mut i = SMALL_0;
        while (i < ACTIONS) { k6(uid, i, ctx); i = i + SMALL_1; };
    }
    /// Package helper: update.
    public(package) fun k10(uid: &mut UID, requirements: vector<u8>, ctx: &TxContext) {
        z0(requirements.length() == ACTIONS);
        let current = k23(uid);
        k6(uid, SMALL_4, ctx);
        let mut j = SMALL_0;
        while (j < ACTIONS) {
            if ((current.requirements[j] & requirements[j]) != current.requirements[j]) k6(uid, j, ctx);
            j = j + SMALL_1;
        };
        let mut i = SMALL_0;
        while (i < ACTIONS) {
            z0((requirements[i] & 15) == requirements[i]);
            assert!((requirements[i] & AVAILABLE) == requirements[i], EUnavailable);
            let mut bit = 1;
            while (bit <= 8) {
                if ((requirements[i] & bit) != 0) z2(!k4(uid, bit).is_empty());
                bit = bit * 2;
            };
            i = i + SMALL_1;
        };
        let customer_id = uid.to_inner();
        let p = df::borrow_mut<PolicyKey, Policy>(uid, PolicyKey {});
        p.requirements = requirements;
        p.generation = p.generation + SMALL_1;
        p.transaction = v0();
        p.attested = 0;
        event::emit(PolicyChanged { customer_id, generation: p.generation, requirements });
    }
    /// Credential/configuration mutation invalidates every queued authorization.
    /// Package helper: invalidate.
    public(package) fun k11(uid: &mut UID) {
        if (df::exists(uid, DelayGrantKey {})) { df::remove<DelayGrantKey, DelayGrant>(uid, DelayGrantKey {}); };
        let p = df::borrow_mut<PolicyKey, Policy>(uid, PolicyKey {});
        p.generation = p.generation + SMALL_1;
        p.transaction = v0();
        p.attested = 0;
    }
    /// Newly introduced type: old packages cannot create, mutate or consume it.
    /// No store or public transfer: an action remains shared until execution or
    /// cancellation; child modules verify proofs against its immutable intent.
    public struct Action has key {
        id: UID, customer_id: ID, generation: u64, proposer: address,
        intent: vector<u8>, expires_ms: u64, attested: u8,
    }
    // Reserved factor 0 is not configurable. On Customer it indexes pending
    // Action IDs; on Action it holds the disclosed public argument bytes.
    /// Package helper: pending.
    public(package) fun k12(uid: &UID): vector<ID> {
        if (df::exists(uid, FactorKey { factor: 0 })) *df::borrow<FactorKey, vector<ID>>(uid, FactorKey { factor: 0 }) else v0()
    }
    /// Private helper: unindex.
    fun h1(uid: &mut UID, id: ID) {
        if (df::exists(uid, FactorKey { factor: 0 })) {
            let ids = df::borrow_mut<FactorKey, vector<ID>>(uid, FactorKey { factor: 0 });
            let (found, i) = ids.index_of(&id);
            if (found) { ids.remove(i); };
        };
    }
    public fun disclosed_arguments(a: &Action): vector<u8> {
        df::borrow<FactorKey, FactorConfiguration>(&a.id, FactorKey { factor: 0 }).binding
    }
    public fun attestations(a: &Action): u8 { a.attested }
    /// Private helper: remove_arguments.
    fun h2(uid: &mut UID) {
        if (df::exists(uid, FactorKey { factor: 0 })) { df::remove<FactorKey, FactorConfiguration>(uid, FactorKey { factor: 0 }); };
    }
    /// Package helper: prepare_disclosed.
    public(package) fun k13(uid: &mut UID, arguments_bcs: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        z0(!arguments_bcs.is_empty()); z0(arguments_bcs.length() <= 16384);
        if (!df::exists(uid, FactorKey { factor: 0 })) df::add(uid, FactorKey { factor: 0 }, vector<ID>[]);
        z0(df::borrow<FactorKey, vector<ID>>(uid, FactorKey { factor: 0 }).length() < 64);
        let mut a = h3(uid, action_intent(uid.to_inner(), arguments_bcs), expires_ms, clock, ctx);
        df::add(&mut a.id, FactorKey { factor: 0 }, FactorConfiguration { binding: arguments_bcs });
        let id = object::id(&a);
        transfer::share_object(a);
        df::borrow_mut<FactorKey, vector<ID>>(uid, FactorKey { factor: 0 }).push_back(id);
    }
    public struct ActionCreated has copy, drop {
        action_id: ID, customer_id: ID, generation: u64, proposer: address,
        intent: vector<u8>, expires_ms: u64,
    }
    public struct ArgumentsKey has copy, drop, store {}
    public struct ActionArguments has copy, drop, store { bytes: vector<u8> }
    /// Package helper: prepare_with_arguments.
    public(package) fun k14(uid: &UID, bytes: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        z0(bytes.length() <= 65536);
        let intent = action_intent(uid.to_inner(), bytes);
        let mut a = h3(uid, intent, expires_ms, clock, ctx);
        df::add(&mut a.id, ArgumentsKey {}, ActionArguments { bytes });
        transfer::share_object(a);
    }
    /// Package helper: prepare.
    public(package) fun k15(uid: &UID, intent: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        transfer::share_object(h3(uid, intent, expires_ms, clock, ctx));
    }
    /// Private helper: new_action.
    fun h3(uid: &UID, intent: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext): Action {
        z0(intent.length() == SMALL_32);
        let now = clock.timestamp_ms();
        z0(expires_ms > now); z0(expires_ms - now <= SMALL_604800000);
        let p = k23(uid);
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
    /// Package helper: attest.
    public(package) fun k16(a: &mut Action, factor: u8) {
        if (!(factor == 1)) { if (!(factor == 2)) { if (!(factor == 4)) { z2(factor == 8); }; }; };
        a.attested = a.attested | factor;
    }
    /// Each protected wrapper computes its own canonical intent, consumes the
    /// action, performs one operation, then clears this internal stack-scoped
    /// grant before returning to the PTB. No public begin/end entry points.
    /// Package helper: begin.
    public(package) fun k17(uid: &mut UID, a: Action, arguments: vector<vector<u8>>, clock: &Clock, ctx: &TxContext) {
        k18(uid, a, arguments, SMALL_7, false, clock, ctx);
    }
    /// Class 7 is reserved for changes that require the union of current policy.
    /// Pause skips only delay; it cannot alter schedule, recipients or targets.
    /// Package helper: begin_scoped.
    public(package) fun k18(uid: &mut UID, a: Action, arguments: vector<vector<u8>>, class: u64, pause: bool, clock: &Clock, ctx: &TxContext) {
        k19(uid, a, bcs::to_bytes(&arguments), class, pause, clock, ctx);
    }
    /// Package helper: begin_scoped_bytes.
    public(package) fun k19(uid: &mut UID, a: Action, arguments_bcs: vector<u8>, class: u64, pause: bool, clock: &Clock, ctx: &TxContext) {
        z0(class <= ACTIONS); z0((!pause || class == SMALL_1));
        let mut a = a;
        let current = k23(uid);
        let mut delay = SMALL_0;
        let mut required = 0;
        let durations = k1(uid);
        let mut j = SMALL_0;
        while (j < ACTIONS) {
            if (class == ACTIONS || class == j) {
                required = required | current.requirements[j];
                if (!pause && (current.requirements[j] & 8) != 0) {
                    z1(durations.length() == ACTIONS);
                    if (durations[j] > delay) delay = durations[j];
                };
            };
            j = j + SMALL_1;
        };
        let elapsed_ms = if (df::exists(&a.id, QueuedAtKey {})) {
            let created = k2(&a);
            z1(clock.timestamp_ms() >= created);
            clock.timestamp_ms() - created
        } else { z1(delay == SMALL_0); SMALL_0 };
        z1(elapsed_ms >= delay);
        if (df::exists(uid, DelayGrantKey {})) { df::remove<DelayGrantKey, DelayGrant>(uid, DelayGrantKey {}); };
        df::add(uid, DelayGrantKey {}, DelayGrant { elapsed_ms, transaction: *ctx.digest(), pause });
        h0(&mut a.id);
        let Action { mut id, customer_id, generation, proposer, intent, expires_ms, attested } = a;
        // Timing is rechecked at consumption, never a persistent attestation.
        let attested = attested | 8;
        z0(customer_id == uid.to_inner()); z0(proposer == ctx.sender());
        z0(clock.timestamp_ms() < expires_ms);
        z0(intent == action_intent(uid.to_inner(), arguments_bcs));
        let authenticated = h5(uid, ctx);
        let p = df::borrow_mut<PolicyKey, Policy>(uid, PolicyKey {});
        z0(p.schema == SMALL_1); z0(p.generation == generation);
        if (pause) required = required & 7;
        z2(((attested | authenticated) & required) == required);
        p.transaction = *ctx.digest();
        p.attested = attested;
        h1(uid, id.to_inner());
        h2(&mut id);
        h0(&mut id);
        id.delete();
    }
    /// Package helper: finish.
    public(package) fun k20(uid: &mut UID) { k11(uid); }
    /// Package helper: cancel_disclosed.
    public(package) fun k21(uid: &mut UID, a: Action) {
        h1(uid, object::id(&a));
        h4(uid.to_inner(), a);
    }
    /// Package helper: cancel.
    public(package) fun k22(uid: &UID, a: Action) {
        // The legacy immutable route cannot maintain the live pending index.
        z0(!df::exists(&a.id, FactorKey { factor: 0 }));
        h4(uid.to_inner(), a);
    }
    /// Private helper: cancel_internal.
    fun h4(customer: ID, a: Action) {
        let Action { mut id, customer_id, generation: _, proposer: _, intent: _, expires_ms: _, attested: _ } = a;
        z0(customer_id == customer);
        h2(&mut id);
        h0(&mut id);
        id.delete();
    }
    /// Package helper: read.
    public(package) fun k23(uid: &UID): Policy { *df::borrow<PolicyKey, Policy>(uid, PolicyKey {}) }
    public fun generation(p: &Policy): u64 { p.generation }
    public fun requirements(p: &Policy): vector<u8> { p.requirements }

    #[test_only]
    public fun remove_for_testing(uid: &mut UID) { df::remove<PolicyKey, Policy>(uid, PolicyKey {}); }

    #[test_only]
    public fun set_for_testing(uid: &mut UID, requirements: vector<u8>) {
        z0(requirements.length() == ACTIONS);
        df::borrow_mut<PolicyKey, Policy>(uid, PolicyKey {}).requirements = requirements;
    }
    // Separate guard calls retain short-circuit order and the same abort site/code.
    // Avoid materializing intermediate booleans in the caller bytecode.
    // Share each named abort branch without changing its module or error code.

    // Share vector construction bytecode; element order and encoding are unchanged.
    fun v0<T>(): vector<T> { vector[] }

    // Private named guards retain module-local error codes; selectors are fixed at each call.

    fun z0(condition: bool) { if (!condition) abort EPolicy }
    fun z1(condition: bool) { if (!condition) abort EDelay }
    fun z2(condition: bool) { if (!condition) abort EFactor }
}
