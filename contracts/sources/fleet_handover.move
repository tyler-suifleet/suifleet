/// Explicit two-party, resumable authority handover. No key recovery.
module sui_edge::fleet_handover {
    const SMALL_512: u16 = 512;

    // Widen named narrow literals at use sites to avoid repeated LdU64.
    // The pinned compiler uses LdU8/U16/U32 followed by CastU64. Values and u64 types
    // are unchanged; abort constants retain their original u64 declarations.
    const SMALL_0: u8 = 0;
    const SMALL_1: u8 = 1;
    const SMALL_2: u8 = 2;
    const SMALL_3: u8 = 3;
    const SMALL_4: u8 = 4;

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
        p2(recipient != ctx.sender()); p2(recipient != @0x0);
        let n = objects.length();
        p1(n > (SMALL_0 as u64)); p1(n <= (SMALL_512 as u64)); p1(kinds.length() == n);
        g0(expires_ms > clock.timestamp_ms());
        let mut completed = vector[];
        let mut i = (SMALL_0 as u64);
        while (i < n) {
            p1(kinds[i] <= (SMALL_4 as u64));
            let mut j = (SMALL_0 as u64);
            while (j < i) { p1(objects[j] != objects[i]); j = j + (SMALL_1 as u64); };
            completed.push_back(false);
            i = i + (SMALL_1 as u64);
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
        p2(ctx.sender() == h.recipient);
        p3(h.state == 0);
        g0(clock.timestamp_ms() < h.expires_ms);
        h.state = 1;
        event::emit(StateChanged { handover_id: object::id(h), state: h.state });
    }

    public fun cancel(h: &mut Handover, ctx: &TxContext) {
        if (!(ctx.sender() == h.source)) { p2(ctx.sender() == h.recipient); };
        p3(h.state < 2);
        h.state = 2;
        event::emit(StateChanged { handover_id: object::id(h), state: h.state });
    }

    /// step: compact private symbol to bound published bytecode storage.
    fun p0(h: &mut Handover, id: ID, kind: u64, clock: &Clock, ctx: &TxContext) {
        p2(ctx.sender() == h.source);
        p3(h.state == 1);
        g0(clock.timestamp_ms() < h.expires_ms);
        let (found, i) = h.objects.index_of(&id);
        p1(found);
        p1(h.kinds[i] == kind); p1(!h.completed[i]);
        *h.completed.borrow_mut(i) = true;
        event::emit(Progress { handover_id: object::id(h), object_id: id, kind });
    }

    public fun device(h: &mut Handover, cap: DeviceOwnerCap, clock: &Clock, ctx: &TxContext) {
        p0(h, object::id(&cap), (SMALL_0 as u64), clock, ctx);
        // Owned-object input authorization proves the source owns this cap.
        transfer::public_transfer(cap, h.recipient);
    }

    public fun group(h: &mut Handover, group: &mut DeviceGroup, clock: &Clock, ctx: &TxContext) {
        p0(h, object::id(group), (SMALL_1 as u64), clock, ctx);
        device_group::handover(group, h.recipient, ctx);
    }

    public fun deployment(h: &mut Handover, record: &mut DeploymentRecord, clock: &Clock, ctx: &TxContext) {
        p0(h, object::id(record), (SMALL_2 as u64), clock, ctx);
        deployment_manager::handover(record, h.recipient, ctx);
    }

    public fun revoke_role(h: &mut Handover, registry: &mut DeviceRegistry,
        role_id: ID, clock: &Clock, ctx: &TxContext,
    ) {
        p0(h, role_id, (SMALL_3 as u64), clock, ctx);
        roles::revoke(registry, role_id, ctx);
    }

    public fun close_enrollment(h: &mut Handover, policy: &mut EnrollmentPolicy,
        clock: &Clock, ctx: &TxContext,
    ) {
        p0(h, object::id(policy), (SMALL_4 as u64), clock, ctx);
        enrollment::close(policy, ctx);
    }

    public fun finish(h: &mut Handover, ctx: &TxContext) {
        if (!(ctx.sender() == h.source)) { p2(ctx.sender() == h.recipient); };
        p3(h.state == 1);
        p1(!h.completed.contains(&false));
        h.state = 3;
        event::emit(StateChanged { handover_id: object::id(h), state: h.state });
    }

    public fun state(h: &Handover): u8 { h.state }
    public fun completed(h: &Handover): vector<bool> { h.completed }
    // Separate guard calls retain short-circuit order and the same abort site/code.
    // Avoid materializing intermediate booleans in the caller bytecode.
    // Share each named abort branch without changing its module or error code.
    /// check_Inventory: compact private symbol to bound published bytecode storage.
    fun p1(condition: bool) {
        if (!condition) abort EInventory
    }
    /// check_Party: compact private symbol to bound published bytecode storage.
    fun p2(condition: bool) {
        if (!condition) abort EParty
    }
    /// check_State: compact private symbol to bound published bytecode storage.
    fun p3(condition: bool) {
        if (!condition) abort EState
    }

    /// Shared EExpired guard; preserve evaluation order and abort code.
    fun g0(condition: bool) { if (!condition) abort EExpired }
}
