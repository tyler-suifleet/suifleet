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
        assert!(recipient != ctx.sender() && recipient != @0x0, EParty);
        let n = objects.length();
        assert!(n > 0 && n <= 512 && kinds.length() == n, EInventory);
        assert!(expires_ms > clock.timestamp_ms(), EExpired);
        let mut completed = vector[];
        let mut i = 0;
        while (i < n) {
            assert!(kinds[i] <= 4, EInventory);
            let mut j = 0;
            while (j < i) { assert!(objects[j] != objects[i], EInventory); j = j + 1; };
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
        assert!(ctx.sender() == h.source || ctx.sender() == h.recipient, EParty);
        assert!(h.state < 2, EState);
        h.state = 2;
        event::emit(StateChanged { handover_id: object::id(h), state: h.state });
    }

    fun step(h: &mut Handover, id: ID, kind: u64, clock: &Clock, ctx: &TxContext) {
        assert!(ctx.sender() == h.source, EParty);
        assert!(h.state == 1, EState);
        assert!(clock.timestamp_ms() < h.expires_ms, EExpired);
        let (found, i) = h.objects.index_of(&id);
        assert!(found, EInventory);
        assert!(h.kinds[i] == kind && !h.completed[i], EInventory);
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
        assert!(ctx.sender() == h.source || ctx.sender() == h.recipient, EParty);
        assert!(h.state == 1, EState);
        assert!(!h.completed.contains(&false), EInventory);
        h.state = 3;
        event::emit(StateChanged { handover_id: object::id(h), state: h.state });
    }

    public fun state(h: &Handover): u8 { h.state }
    public fun completed(h: &Handover): vector<bool> { h.completed }
}
