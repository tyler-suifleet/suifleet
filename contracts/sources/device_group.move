module sui_edge::device_group {
    // Widen named u8 literals at use sites: the pinned compiler emits compact
    // LdU8/CastU64 instead of repeated LdU64. Values and inferred u64 types
    // are unchanged; abort constants retain their original u64 declarations.
    const SMALL_0: u8 = 0;
    const SMALL_1: u8 = 1;

    use std::string::String;
    use sui::clock::Clock;
    use sui::dynamic_field as df;
    use sui::event;
    use sui_edge::device_registry::{Self, Device};

    // ── Constants ─────────────────────────────────────────────────────────────

    const SCHEMA_VERSION: u64 = 1;

    // ── Errors ────────────────────────────────────────────────────────────────

    const ENotAdmin: u64 = 0;
    const EDeviceAlreadyInGroup: u64 = 1;
    const EDeviceNotInGroup: u64 = 2;
    const EInvalidLimit: u64 = 3;

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

    // ── Member index (SF-125) ─────────────────────────────────────────────────
    //
    // Introduced by an upgrade, so older packages can neither read nor write
    // these types. Once the admin indexes a group, members live in MemberKey
    // fields instead of `device_ids`: membership is O(1), the group object
    // stays small and the vector's object-size cap no longer limits the group.
    // Older packages still see only `device_ids`, so index a group only once
    // its devices and tools run SF-125 clients.

    /// Dynamic-field key on an indexed group: present (true) per member.
    public struct MemberKey has copy, drop, store { device_id: ID }
    /// Present once the group is indexed; new members then go to MemberKey.
    public struct IndexKey has copy, drop, store {}
    public struct GroupIndex has store, drop {
        /// Members held in MemberKey fields.
        members: u64,
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

    public struct GroupIndexed has copy, drop {
        group_id: ID,
        moved: u64,
        /// Members still in `device_ids`; index again until it reaches 0.
        remaining: u64,
    }

    // ── Public functions ──────────────────────────────────────────────────────

    public fun create_group(
        name: String,
        description: String,
        clock: &Clock,
        ctx: &mut TxContext,
    ) {
        share_managed(new_managed(name, description, ctx.sender(), clock, ctx));
    }

    #[allow(lint(prefer_mut_tx_context))]
    public fun add_device(
        group: &mut DeviceGroup,
        device: &Device,
        ctx: &TxContext,
    ) {
        p0(ctx.sender() == group.admin);
        add_managed(group, device_registry::device_id(device));
    }

    #[allow(lint(prefer_mut_tx_context))]
    public fun remove_device(
        group: &mut DeviceGroup,
        device_id: ID,
        ctx: &TxContext,
    ) {
        assert!(ctx.sender() == group.admin, ENotAdmin);
        remove_managed(group, device_id);
    }

    #[allow(lint(prefer_mut_tx_context, public_entry))]
    public entry fun delete_group(group: DeviceGroup, ctx: &TxContext) {
        p0(ctx.sender() == group.admin);
        // Remaining MemberKey fields are orphaned with the group; remove
        // members first to reclaim their storage.
        delete_managed(group);
    }

    /// Move up to `limit` members from `device_ids` into the MemberKey index,
    /// marking the group indexed. Admin only; repeat until `remaining` is 0.
    public fun index_members(group: &mut DeviceGroup, limit: u64, ctx: &mut TxContext) {
        p0(ctx.sender() == group.admin);
        assert!(limit > (SMALL_0 as u64), EInvalidLimit);
        if (!is_indexed(group)) {
            df::add(&mut group.id, IndexKey {}, GroupIndex { members: (SMALL_0 as u64) });
        };
        let mut moved = (SMALL_0 as u64);
        while (moved < limit && !group.device_ids.is_empty()) {
            let device_id = group.device_ids.pop_back();
            // An older package may have re-added an indexed member.
            if (!df::exists(&group.id, MemberKey { device_id })) {
                df::add(&mut group.id, MemberKey { device_id }, true);
                let index: &mut GroupIndex = df::borrow_mut(&mut group.id, IndexKey {});
                index.members = index.members + (SMALL_1 as u64);
            };
            moved = moved + (SMALL_1 as u64);
        };
        event::emit(GroupIndexed {
            group_id: object::id(group),
            moved,
            remaining: group.device_ids.length(),
        });
    }

    /// Called only by the recipient-confirmed fleet handover protocol.
    public(package) fun handover(group: &mut DeviceGroup, recipient: address, ctx: &TxContext) {
        p0(ctx.sender() == group.admin);
        group.admin = recipient;
    }

    /// Construct a group with the supplied admin. Managed callers use an object
    /// address: no wallet can sign as it through legacy add/remove/handover.
    public(package) fun new_managed(name: String, description: String, admin: address,
        clock: &Clock, ctx: &mut TxContext): DeviceGroup {
        let group = DeviceGroup { id: object::new(ctx), name, description, admin,
            device_ids: vector[], created_at: clock.timestamp_ms(), schema_version: SCHEMA_VERSION };
        event::emit(GroupCreated { group_id: object::id(&group), name: group.name, admin });
        group
    }
    public(package) fun delete_managed(mut group: DeviceGroup) {
        if (is_indexed(&group)) {
            let _: GroupIndex = df::remove(&mut group.id, IndexKey {});
        };
        let DeviceGroup { id, name: _, description: _, admin, device_ids: _, created_at: _, schema_version: _ } = group;
        event::emit(GroupDeleted { group_id: id.to_inner(), admin });
        id.delete();
    }
    public(package) fun share_managed(group: DeviceGroup) { transfer::share_object(group); }
    public(package) fun add_managed(group: &mut DeviceGroup, device_id: ID) {
        assert!(!contains_device(group, device_id), EDeviceAlreadyInGroup);
        if (is_indexed(group)) {
            df::add(&mut group.id, MemberKey { device_id }, true);
            let index: &mut GroupIndex = df::borrow_mut(&mut group.id, IndexKey {});
            index.members = index.members + (SMALL_1 as u64);
        } else {
            group.device_ids.push_back(device_id);
        };
        event::emit(DeviceAddedToGroup {
            group_id: object::id(group),
            device_id,
        });
    }
    public(package) fun remove_managed(group: &mut DeviceGroup, device_id: ID) {
        // An older package may have added an indexed member to the vector too:
        // remove it from both so it is no longer a member anywhere.
        let indexed = df::exists(&group.id, MemberKey { device_id });
        if (indexed) {
            let _: bool = df::remove(&mut group.id, MemberKey { device_id });
            let index: &mut GroupIndex = df::borrow_mut(&mut group.id, IndexKey {});
            index.members = index.members - (SMALL_1 as u64);
        };
        let (found, idx) = group.device_ids.index_of(&device_id);
        assert!(found || indexed, EDeviceNotInGroup);
        if (found) { group.device_ids.swap_remove(idx); };
        event::emit(DeviceRemovedFromGroup {
            group_id: object::id(group),
            device_id,
        });
    }

    /// Replays an older package's `add_device`, which sees only the vector.
    #[test_only]
    public fun legacy_add_for_testing(group: &mut DeviceGroup, device_id: ID) {
        let (found, _) = group.device_ids.index_of(&device_id);
        assert!(!found, EDeviceAlreadyInGroup);
        group.device_ids.push_back(device_id);
    }

    // ── Read helpers ──────────────────────────────────────────────────────────

    /// O(1) for an indexed group once `index_members` has emptied the vector.
    public fun contains_device(group: &DeviceGroup, device_id: ID): bool {
        if (df::exists(&group.id, MemberKey { device_id })) return true;
        let (found, _) = group.device_ids.index_of(&device_id);
        found
    }

    public fun is_indexed(group: &DeviceGroup): bool { df::exists(&group.id, IndexKey {}) }

    /// Members in `device_ids` plus indexed members.
    public fun member_count(group: &DeviceGroup): u64 {
        let indexed = if (is_indexed(group)) {
            let index: &GroupIndex = df::borrow(&group.id, IndexKey {});
            index.members
        } else { (SMALL_0 as u64) };
        group.device_ids.length() + indexed
    }

    public fun group_id(group: &DeviceGroup): ID { object::id(group) }
    /// Unindexed members only; indexed members are MemberKey fields.
    public fun device_ids(group: &DeviceGroup): &vector<ID> { &group.device_ids }
    public fun name(group: &DeviceGroup): String { group.name }
    public fun admin(group: &DeviceGroup): address { group.admin }

    /// For package modules that keep state on a group (desired_state).
    public(package) fun uid(group: &DeviceGroup): &UID { &group.id }
    public(package) fun uid_mut(group: &mut DeviceGroup): &mut UID { &mut group.id }
    // Share each named abort branch without changing its module or error code.
    /// check_NotAdmin: compact private symbol to bound published bytecode storage.
    fun p0(condition: bool) {
        if (!condition) abort ENotAdmin
    }
}
