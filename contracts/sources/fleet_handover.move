/// Explicit two-party, resumable authority handover. No key recovery.
module sui_edge::fleet_handover {
    use sui::clock::Clock;
    use sui::event;
    use sui_edge::device_group::{Self, DeviceGroup};
    use sui_edge::device_registry::{DeviceOwnerCap, DeviceRegistry};
    use sui_edge::roles;
    use sui_edge::enrollment::{Self, EnrollmentPolicy};
    use sui_edge::deployment_manager::{Self, DeploymentRecord};

    const EParty: u64 = 0;
    const EState: u64 = 1;
    const EInventory: u64 = 2;
    const EExpired: u64 = 3;

    /// Kinds: 0 DeviceOwnerCap, 1 DeviceGroup, 2 DeploymentRecord,
    /// 3 revoked DeployerRole ID, 4 permanently closed EnrollmentPolicy.
    /// Completed items remain recorded; cancellation never undoes a completed
    /// transfer. Each step is atomic, including its progress marker.
    public struct Handover has key {
        id: UID,
        source: address,
        recipient: address,
        objects: vector<ID>,
        kinds: vector<u64>,
        completed: vector<bool>,
        expires_ms: u64,
        state: u8, // 0 proposed, 1 confirmed, 2 cancelled, 3 finished
    }

    public struct Proposed has copy, drop {
        handover_id: ID, source: address, recipient: address,
        objects: vector<ID>, kinds: vector<u64>, expires_ms: u64,
    }
    public struct Progress has copy, drop {
        handover_id: ID, object_id: ID, kind: u64,
    }
    public struct StateChanged has copy, drop { handover_id: ID, state: u8 }

    public fun propose(recipient: address, objects: vector<ID>, kinds: vector<u64>,
        expires_ms: u64, clock: &Clock, ctx: &mut TxContext,
    ) {
        check_Party(recipient != ctx.sender() && recipient != @0x0);
        let n = objects.length();
        check_Inventory(n > 0 && n <= 512 && kinds.length() == n);
        assert!(expires_ms > clock.timestamp_ms(), EExpired);
        let mut completed = vector[];
        let mut i = 0;
        while (i < n) {
            check_Inventory(kinds[i] <= 4);
            let mut j = 0;
            while (j < i) { check_Inventory(objects[j] != objects[i]); j = j + 1; };
            completed.push_back(false);
            i = i + 1;
        };
        let h = Handover { id: object::new(ctx), source: ctx.sender(), recipient,
            objects, kinds, completed, expires_ms, state: 0 };
        event::emit(Proposed { handover_id: object::id(&h), source: h.source,
            recipient, objects: h.objects, kinds: h.kinds, expires_ms });
        transfer::share_object(h);
    }

    /// The recipient signs the transaction, including a native Sui multisig
    /// sender. Confirmation cannot be inferred from a source signature.
    public fun confirm(h: &mut Handover, clock: &Clock, ctx: &TxContext) {
        assert!(ctx.sender() == h.recipient, EParty);
        assert!(h.state == 0, EState);
        assert!(clock.timestamp_ms() < h.expires_ms, EExpired);
        h.state = 1;
        event::emit(StateChanged { handover_id: object::id(h), state: h.state });
    }

    public fun cancel(h: &mut Handover, ctx: &TxContext) {
        check_Party(ctx.sender() == h.source || ctx.sender() == h.recipient);
        check_State(h.state < 2);
        h.state = 2;
        event::emit(StateChanged { handover_id: object::id(h), state: h.state });
    }

    fun step(h: &mut Handover, id: ID, kind: u64, clock: &Clock, ctx: &TxContext) {
        check_Party(ctx.sender() == h.source);
        check_State(h.state == 1);
        assert!(clock.timestamp_ms() < h.expires_ms, EExpired);
        let (found, i) = h.objects.index_of(&id);
        check_Inventory(found);
        check_Inventory(h.kinds[i] == kind && !h.completed[i]);
        *h.completed.borrow_mut(i) = true;
        event::emit(Progress { handover_id: object::id(h), object_id: id, kind });
    }

    public fun device(h: &mut Handover, cap: DeviceOwnerCap, clock: &Clock, ctx: &TxContext) {
        step(h, object::id(&cap), 0, clock, ctx);
        // Owned-object input authorization proves the source owns this cap.
        transfer::public_transfer(cap, h.recipient);
    }

    public fun group(h: &mut Handover, group: &mut DeviceGroup, clock: &Clock, ctx: &TxContext) {
        step(h, object::id(group), 1, clock, ctx);
        device_group::handover(group, h.recipient, ctx);
    }

    public fun deployment(h: &mut Handover, record: &mut DeploymentRecord, clock: &Clock, ctx: &TxContext) {
        step(h, object::id(record), 2, clock, ctx);
        deployment_manager::handover(record, h.recipient, ctx);
    }

    public fun revoke_role(h: &mut Handover, registry: &mut DeviceRegistry,
        role_id: ID, clock: &Clock, ctx: &TxContext,
    ) {
        step(h, role_id, 3, clock, ctx);
        roles::revoke(registry, role_id, ctx);
    }

    public fun close_enrollment(h: &mut Handover, policy: &mut EnrollmentPolicy,
        clock: &Clock, ctx: &TxContext,
    ) {
        step(h, object::id(policy), 4, clock, ctx);
        enrollment::close(policy, ctx);
    }

    public fun finish(h: &mut Handover, ctx: &TxContext) {
        check_Party(ctx.sender() == h.source || ctx.sender() == h.recipient);
        check_State(h.state == 1);
        check_Inventory(!h.completed.contains(&false));
        h.state = 3;
        event::emit(StateChanged { handover_id: object::id(h), state: h.state });
    }

    public fun state(h: &Handover): u8 { h.state }
    public fun completed(h: &Handover): vector<bool> { h.completed }
    // Share each named abort branch without changing its module or error code.
    fun check_Inventory(condition: bool) {
        if (!condition) abort EInventory
    }
    fun check_Party(condition: bool) {
        if (!condition) abort EParty
    }
    fun check_State(condition: bool) {
        if (!condition) abort EState
    }
}
