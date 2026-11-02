/// Shared second-factor enforcement. Only reviewed in-package mechanism modules
/// may attest; a public caller cannot manufacture a factor or a transaction grant.
module sui_edge::security_policy {
    use std::bcs;
    use sui::dynamic_field as df;
    use sui::event;
    use sui::clock::Clock;
    use sui::hash;

    const EPolicy: u64 = 0;
    const EFactor: u64 = 1;
    const EUnavailable: u64 = 2;
    const ACTIONS: u64 = 7;
    // Mechanism implementations land separately in SF-198/199/200/201. Keep
    // unsupported options unavailable, rather than accepting unverified proofs.
    const AVAILABLE: u8 = 4;

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
        assert!(factor == 1 || factor == 2 || factor == 4 || factor == 8, EFactor);
        check_all(uid, ctx);
        assert!(!binding.is_empty() && binding.length() <= 4096, EPolicy);
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
        assert!(!df::exists(uid, PolicyKey {}), EPolicy);
        df::add(uid, PolicyKey {}, Policy { schema: 1, generation: 0,
            requirements: vector[0, 0, 0, 0, 0, 0, 0], transaction: vector[], attested: 0 });
    }
    public(package) fun check(uid: &UID, action: u64, ctx: &TxContext) {
        let p = df::borrow<PolicyKey, Policy>(uid, PolicyKey {});
        assert!(p.schema == 1 && action < ACTIONS, EPolicy);
        let needed = p.requirements[action];
        assert!(needed == 0 || (p.transaction == *ctx.digest() && (p.attested & needed) == needed), EFactor);
    }
    /// Capability lending cannot be authorized by a wrapper grant: a caller
    /// could otherwise substitute any operation while holding the capability.
    public(package) fun assert_unprotected(uid: &UID) {
        let p = read(uid);
        let mut i = 0;
        while (i < ACTIONS) { assert!(p.requirements[i] == 0, EFactor); i = i + 1; };
    }
    public(package) fun check_all(uid: &UID, ctx: &TxContext) {
        let mut i = 0;
        while (i < ACTIONS) { check(uid, i, ctx); i = i + 1; };
    }
    public(package) fun update(uid: &mut UID, requirements: vector<u8>, ctx: &TxContext) {
        assert!(requirements.length() == ACTIONS, EPolicy);
        let current = read(uid);
        check(uid, 4, ctx);
        let mut j = 0;
        while (j < ACTIONS) {
            if ((current.requirements[j] & requirements[j]) != current.requirements[j]) check(uid, j, ctx);
            j = j + 1;
        };
        let mut i = 0;
        while (i < ACTIONS) {
            assert!((requirements[i] & 15) == requirements[i], EPolicy);
            assert!((requirements[i] & AVAILABLE) == requirements[i], EUnavailable);
            let mut bit = 1;
            while (bit <= 8) {
                if ((requirements[i] & bit) != 0) assert!(!factor_configuration(uid, bit).is_empty(), EFactor);
                bit = bit * 2;
            };
            i = i + 1;
        };
        let customer_id = uid.to_inner();
        let p = df::borrow_mut<PolicyKey, Policy>(uid, PolicyKey {});
        p.requirements = requirements;
        p.generation = p.generation + 1;
        p.transaction = vector[];
        p.attested = 0;
        event::emit(PolicyChanged { customer_id, generation: p.generation, requirements });
    }
    /// Credential/configuration mutation invalidates every queued authorization.
    public(package) fun invalidate(uid: &mut UID) {
        let p = df::borrow_mut<PolicyKey, Policy>(uid, PolicyKey {});
        p.generation = p.generation + 1;
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
    // Reserved factor 0 is not configurable. On Customer it indexes pending
    // Action IDs; on Action it holds the disclosed public argument bytes.
    public(package) fun pending(uid: &UID): vector<ID> {
        if (df::exists(uid, FactorKey { factor: 0 })) *df::borrow<FactorKey, vector<ID>>(uid, FactorKey { factor: 0 }) else vector[]
    }
    fun unindex(uid: &mut UID, id: ID) {
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
    fun remove_arguments(uid: &mut UID) {
        if (df::exists(uid, FactorKey { factor: 0 })) { df::remove<FactorKey, FactorConfiguration>(uid, FactorKey { factor: 0 }); };
    }
    public(package) fun prepare_disclosed(uid: &mut UID, arguments_bcs: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        assert!(!arguments_bcs.is_empty() && arguments_bcs.length() <= 16384, EPolicy);
        if (!df::exists(uid, FactorKey { factor: 0 })) df::add(uid, FactorKey { factor: 0 }, vector<ID>[]);
        assert!(df::borrow<FactorKey, vector<ID>>(uid, FactorKey { factor: 0 }).length() < 64, EPolicy);
        let id = prepare_internal(uid, action_intent(uid.to_inner(), arguments_bcs), expires_ms, arguments_bcs, clock, ctx);
        df::borrow_mut<FactorKey, vector<ID>>(uid, FactorKey { factor: 0 }).push_back(id);
    }
    public struct ActionCreated has copy, drop {
        action_id: ID, customer_id: ID, generation: u64, proposer: address,
        intent: vector<u8>, expires_ms: u64,
    }
    public(package) fun prepare(uid: &UID, intent: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        let _ = prepare_internal(uid, intent, expires_ms, vector[], clock, ctx);
    }
    fun prepare_internal(uid: &UID, intent: vector<u8>, expires_ms: u64, disclosed: vector<u8>, clock: &Clock, ctx: &mut TxContext): ID {
        assert!(intent.length() == 32, EPolicy);
        let now = clock.timestamp_ms();
        assert!(expires_ms > now && expires_ms - now <= 604800000, EPolicy);
        let p = read(uid);
        let mut a = Action { id: object::new(ctx), customer_id: uid.to_inner(), generation: p.generation,
            proposer: ctx.sender(), intent, expires_ms, attested: 0 };
        if (!disclosed.is_empty()) df::add(&mut a.id, FactorKey { factor: 0 }, FactorConfiguration { binding: disclosed });
        event::emit(ActionCreated { action_id: object::id(&a), customer_id: a.customer_id,
            generation: a.generation, proposer: a.proposer, intent, expires_ms });
        let id = object::id(&a);
        transfer::share_object(a);
        id
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
        assert!(factor == 1 || factor == 2 || factor == 4 || factor == 8, EFactor);
        a.attested = a.attested | factor;
    }
    /// Each protected wrapper computes its own canonical intent, consumes the
    /// action, performs one operation, then clears this internal stack-scoped
    /// grant before returning to the PTB. No public begin/end entry points.
    public(package) fun begin(uid: &mut UID, a: Action, arguments: vector<vector<u8>>, clock: &Clock, ctx: &TxContext) {
        begin_scoped(uid, a, arguments, 7, false, clock, ctx);
    }
    /// Class 7 is reserved for changes that require the union of current policy.
    /// Pause skips only delay; it cannot alter schedule, recipients or targets.
    public(package) fun begin_scoped(uid: &mut UID, a: Action, arguments: vector<vector<u8>>, class: u64, pause: bool, clock: &Clock, ctx: &TxContext) {
        begin_scoped_bytes(uid, a, bcs::to_bytes(&arguments), class, pause, clock, ctx);
    }
    public(package) fun begin_scoped_bytes(uid: &mut UID, a: Action, arguments_bcs: vector<u8>, class: u64, pause: bool, clock: &Clock, ctx: &TxContext) {
        assert!(class <= ACTIONS && (!pause || class == 1), EPolicy);
        let Action { mut id, customer_id, generation, proposer, intent, expires_ms, attested } = a;
        assert!(customer_id == uid.to_inner() && proposer == ctx.sender(), EPolicy);
        assert!(clock.timestamp_ms() < expires_ms, EPolicy);
        assert!(intent == action_intent(uid.to_inner(), arguments_bcs), EPolicy);
        let p = df::borrow_mut<PolicyKey, Policy>(uid, PolicyKey {});
        assert!(p.schema == 1 && p.generation == generation, EPolicy);
        let mut required = 0;
        let mut i = 0;
        while (i < ACTIONS) {
            if (class == ACTIONS || class == i) required = required | p.requirements[i];
            i = i + 1;
        };
        if (pause) required = required & 7;
        assert!((attested & required) == required, EFactor);
        p.transaction = *ctx.digest();
        p.attested = if (pause) attested | 8 else attested;
        unindex(uid, id.to_inner());
        remove_arguments(&mut id);
        id.delete();
    }
    public(package) fun finish(uid: &mut UID) { invalidate(uid); }
    public(package) fun cancel_disclosed(uid: &mut UID, a: Action) {
        unindex(uid, object::id(&a));
        cancel_internal(uid.to_inner(), a);
    }
    public(package) fun cancel(uid: &UID, a: Action) {
        // The legacy immutable route cannot maintain the live pending index.
        assert!(!df::exists(&a.id, FactorKey { factor: 0 }), EPolicy);
        cancel_internal(uid.to_inner(), a);
    }
    fun cancel_internal(customer: ID, a: Action) {
        let Action { mut id, customer_id, generation: _, proposer: _, intent: _, expires_ms: _, attested: _ } = a;
        assert!(customer_id == customer, EPolicy);
        remove_arguments(&mut id);
        id.delete();
    }
    public(package) fun read(uid: &UID): Policy { *df::borrow<PolicyKey, Policy>(uid, PolicyKey {}) }
    public fun generation(p: &Policy): u64 { p.generation }
    public fun requirements(p: &Policy): vector<u8> { p.requirements }

    #[test_only]
    public fun remove_for_testing(uid: &mut UID) { df::remove<PolicyKey, Policy>(uid, PolicyKey {}); }

    #[test_only]
    public fun set_for_testing(uid: &mut UID, requirements: vector<u8>) {
        assert!(requirements.length() == ACTIONS, EPolicy);
        df::borrow_mut<PolicyKey, Policy>(uid, PolicyKey {}).requirements = requirements;
    }
}
