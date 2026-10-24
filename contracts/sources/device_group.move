module sui_edge::device_group {
    use std::string::String;
    use sui::clock::Clock;
    use sui::event;
    use sui_edge::device_registry::{Self, Device};

    // ── Constants ─────────────────────────────────────────────────────────────

    const SCHEMA_VERSION: u64 = 1;

    // ── Errors ────────────────────────────────────────────────────────────────

    const ENotAdmin: u64 = 0;
    const EDeviceAlreadyInGroup: u64 = 1;
    const EDeviceNotInGroup: u64 = 2;

    // ── Objects ───────────────────────────────────────────────────────────────

    /// Shared object. Represents a named fleet of devices that receive
    /// deployments together.
    public struct DeviceGroup has key {
        id: UID,
        name: String,
        description: String,
        admin: address,
        device_ids: vector<ID>,
        created_at: u64,
        /// Monotonic version bump used to gate future migrations.
        schema_version: u64,
    }

    // ── Events ────────────────────────────────────────────────────────────────

    public struct GroupCreated has copy, drop {
        group_id: ID,
        name: String,
        admin: address,
    }

    public struct DeviceAddedToGroup has copy, drop {
        group_id: ID,
        device_id: ID,
    }

    public struct DeviceRemovedFromGroup has copy, drop {
        group_id: ID,
        device_id: ID,
    }

    public struct GroupDeleted has copy, drop {
        group_id: ID,
        admin: address,
    }

    // ── Public functions ──────────────────────────────────────────────────────

    public fun create_group(
        name: String,
        description: String,
        clock: &Clock,
        ctx: &mut TxContext,
    ) {
        let group = DeviceGroup {
            id: object::new(ctx),
            name,
            description,
            admin: ctx.sender(),
            device_ids: vector[],
            created_at: clock.timestamp_ms(),
            schema_version: SCHEMA_VERSION,
        };
        event::emit(GroupCreated {
            group_id: object::id(&group),
            name: group.name,
            admin: ctx.sender(),
        });
        transfer::share_object(group);
    }

    #[allow(lint(prefer_mut_tx_context))]
    public fun add_device(
        group: &mut DeviceGroup,
        device: &Device,
        ctx: &TxContext,
    ) {
        assert!(ctx.sender() == group.admin, ENotAdmin);
        let device_id = device_registry::device_id(device);
        let (found, _) = group.device_ids.index_of(&device_id);
        assert!(!found, EDeviceAlreadyInGroup);
        group.device_ids.push_back(device_id);
        event::emit(DeviceAddedToGroup {
            group_id: object::id(group),
            device_id,
        });
    }

    #[allow(lint(prefer_mut_tx_context))]
    public fun remove_device(
        group: &mut DeviceGroup,
        device_id: ID,
        ctx: &TxContext,
    ) {
        assert!(ctx.sender() == group.admin, ENotAdmin);
        let (found, idx) = group.device_ids.index_of(&device_id);
        assert!(found, EDeviceNotInGroup);
        group.device_ids.swap_remove(idx);
        event::emit(DeviceRemovedFromGroup {
            group_id: object::id(group),
            device_id,
        });
    }

    #[allow(lint(prefer_mut_tx_context, public_entry))]
    public entry fun delete_group(group: DeviceGroup, ctx: &TxContext) {
        assert!(ctx.sender() == group.admin, ENotAdmin);
        let DeviceGroup { id, name: _, description: _, admin, device_ids: _, created_at: _, schema_version: _ } = group;
        event::emit(GroupDeleted { group_id: id.to_inner(), admin });
        id.delete();
    }

    /// Called only by the recipient-confirmed fleet handover protocol.
    public(package) fun handover(group: &mut DeviceGroup, recipient: address, ctx: &TxContext) {
        assert!(ctx.sender() == group.admin, ENotAdmin);
        group.admin = recipient;
    }

    /// Managed groups use an object address as admin. No wallet can sign as
    /// that address, sealing legacy add/remove/handover entry points.
    public(package) fun new_managed(name: String, description: String, admin: address,
        clock: &Clock, ctx: &mut TxContext): DeviceGroup {
        let group = DeviceGroup { id: object::new(ctx), name, description, admin,
            device_ids: vector[], created_at: clock.timestamp_ms(), schema_version: SCHEMA_VERSION };
        event::emit(GroupCreated { group_id: object::id(&group), name: group.name, admin });
        group
    }
    public(package) fun delete_managed(group: DeviceGroup) {
        let DeviceGroup { id, name: _, description: _, admin, device_ids: _, created_at: _, schema_version: _ } = group;
        event::emit(GroupDeleted { group_id: id.to_inner(), admin });
        id.delete();
    }
    public(package) fun share_managed(group: DeviceGroup) { transfer::share_object(group); }
    public(package) fun add_managed(group: &mut DeviceGroup, device_id: ID) {
        assert!(!group.device_ids.contains(&device_id), EDeviceAlreadyInGroup);
        group.device_ids.push_back(device_id);
        event::emit(DeviceAddedToGroup { group_id: object::id(group), device_id });
    }
    public(package) fun remove_managed(group: &mut DeviceGroup, device_id: ID) {
        let (found, idx) = group.device_ids.index_of(&device_id);
        assert!(found, EDeviceNotInGroup);
        group.device_ids.swap_remove(idx);
        event::emit(DeviceRemovedFromGroup { group_id: object::id(group), device_id });
    }

    // ── Read helpers ──────────────────────────────────────────────────────────

    public fun contains_device(group: &DeviceGroup, device_id: ID): bool {
        let (found, _) = group.device_ids.index_of(&device_id);
        found
    }

    public fun group_id(group: &DeviceGroup): ID { object::id(group) }
    public fun device_ids(group: &DeviceGroup): &vector<ID> { &group.device_ids }
    public fun name(group: &DeviceGroup): String { group.name }
    public fun admin(group: &DeviceGroup): address { group.admin }

    /// For package modules that keep state on a group (desired_state).
    public(package) fun uid(group: &DeviceGroup): &UID { &group.id }
    public(package) fun uid_mut(group: &mut DeviceGroup): &mut UID { &mut group.id }
}
