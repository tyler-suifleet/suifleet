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

    public entry fun delete_group(group: DeviceGroup, ctx: &TxContext) {
        assert!(ctx.sender() == group.admin, ENotAdmin);
        let DeviceGroup { id, name: _, description: _, admin, device_ids: _, created_at: _, schema_version: _ } = group;
        event::emit(GroupDeleted { group_id: id.to_inner(), admin });
        id.delete();
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
}
