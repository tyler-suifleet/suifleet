module sui_edge::device_group {
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
        assert!(!contains_device(group, device_id), EDeviceAlreadyInGroup);
        if (is_indexed(group)) {
            df::add(&mut group.id, MemberKey { device_id }, true);
            let index: &mut GroupIndex = df::borrow_mut(&mut group.id, IndexKey {});
            index.members = index.members + 1;
        } else {
            group.device_ids.push_back(device_id);
        };
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
        // An older package may have added an indexed member to the vector too:
        // remove it from both so it is no longer a member anywhere.
        let indexed = df::exists(&group.id, MemberKey { device_id });
        if (indexed) {
            let _: bool = df::remove(&mut group.id, MemberKey { device_id });
            let index: &mut GroupIndex = df::borrow_mut(&mut group.id, IndexKey {});
            index.members = index.members - 1;
        };
        let (found, idx) = group.device_ids.index_of(&device_id);
        assert!(found || indexed, EDeviceNotInGroup);
        if (found) { group.device_ids.swap_remove(idx); };
        event::emit(DeviceRemovedFromGroup {
            group_id: object::id(group),
            device_id,
        });
    }

    #[allow(lint(prefer_mut_tx_context, public_entry))]
    public entry fun delete_group(mut group: DeviceGroup, ctx: &TxContext) {
        assert!(ctx.sender() == group.admin, ENotAdmin);
        // Remaining MemberKey fields are orphaned with the group; remove
        // members first to reclaim their storage.
        if (is_indexed(&group)) {
            let _: GroupIndex = df::remove(&mut group.id, IndexKey {});
        };
        let DeviceGroup { id, name: _, description: _, admin, device_ids: _, created_at: _, schema_version: _ } = group;
        event::emit(GroupDeleted { group_id: id.to_inner(), admin });
        id.delete();
    }

    /// Move up to `limit` members from `device_ids` into the MemberKey index,
    /// marking the group indexed. Admin only; repeat until `remaining` is 0.
    public fun index_members(group: &mut DeviceGroup, limit: u64, ctx: &mut TxContext) {
        assert!(ctx.sender() == group.admin, ENotAdmin);
        assert!(limit > 0, EInvalidLimit);
        if (!is_indexed(group)) {
            df::add(&mut group.id, IndexKey {}, GroupIndex { members: 0 });
        };
        let mut moved = 0;
        while (moved < limit && !group.device_ids.is_empty()) {
            let device_id = group.device_ids.pop_back();
            // An older package may have re-added an indexed member.
            if (!df::exists(&group.id, MemberKey { device_id })) {
                df::add(&mut group.id, MemberKey { device_id }, true);
                let index: &mut GroupIndex = df::borrow_mut(&mut group.id, IndexKey {});
                index.members = index.members + 1;
            };
            moved = moved + 1;
        };
        event::emit(GroupIndexed {
            group_id: object::id(group),
            moved,
            remaining: group.device_ids.length(),
        });
    }

    /// Called only by the recipient-confirmed fleet handover protocol.
    public(package) fun handover(group: &mut DeviceGroup, recipient: address, ctx: &TxContext) {
        assert!(ctx.sender() == group.admin, ENotAdmin);
        group.admin = recipient;
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
        } else { 0 };
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
}
