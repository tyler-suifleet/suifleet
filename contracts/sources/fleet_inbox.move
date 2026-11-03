/// Device inboxes and group heads (SF-125): what a device must read to learn
/// about new deployments, kept in objects rather than reconstructed from
/// event history.
///
/// Every device gets one shared `DeviceInbox` at an address derived from the
/// registry and its device ID, so the device can compute it without a lookup.
/// Every group gets a `Head` dynamic field. The deployment transaction appends
/// an announcement that bumps the sequence, records the deployment as the
/// latest of its component and keeps the earliest future scheduled start, so
/// devices can arm a local timer.
///
/// Announcements are hints, never authority: devices still verify each
/// record's creator, targets and group membership on chain before applying
/// it. Only the record's creator may announce, and only to a group it
/// administers or a device it currently operates. These types are introduced
/// by an upgrade, so older package versions cannot bump heads or inboxes;
/// devices keep a bounded event scan for deployments those versions create.
///
/// The inbox also lists the groups the device was added to (`groups`), so a
/// device can find its groups after the node has pruned the events.
module sui_edge::fleet_inbox {
    // Widen named u8 literals at use sites: the pinned compiler emits compact
    // LdU8/CastU64 instead of repeated LdU64. Values and inferred u64 types
    // are unchanged; abort constants retain their original u64 declarations.
    const SMALL_0: u8 = 0;
    const SMALL_1: u8 = 1;

    use std::string::String;
    use sui::clock::Clock;
    use sui::derived_object;
    use sui::dynamic_field as df;
    use sui::event;
    use sui::vec_map::{Self, VecMap};
    use sui_edge::deployment_manager::{Self, DeploymentRecord};
    use sui_edge::device_group::{Self, DeviceGroup};
    use sui_edge::device_registry::{Self, Device, DeviceOwnerCap, DeviceRegistry};

    // ── Constants ─────────────────────────────────────────────────────────────

    const INBOX_VERSION: u64 = 1;
    /// Components remembered per head; the oldest entry is evicted beyond it.
    const MAX_COMPONENTS: u64 = 64;
    /// Groups listed per inbox.
    const MAX_GROUPS: u64 = 64;

    // ── Errors ────────────────────────────────────────────────────────────────

    const ENotCreator: u64 = 0;
    const ENotAdmin: u64 = 1;
    const ENotTargeted: u64 = 2;
    const ENotOperator: u64 = 3;
    const EWrongInbox: u64 = 4;
    const ETooManyGroups: u64 = 5;
    const ENotMember: u64 = 6;

    // ── State ─────────────────────────────────────────────────────────────────

    /// Derivation key of a device's inbox under the registry.
    public struct InboxKey has copy, drop, store { device_id: ID }

    /// Dynamic-field key of a group's head.
    public struct HeadKey has copy, drop, store {}

    /// What has been announced to a device or group.
    public struct Head has copy, drop, store {
        /// +1 per announcement; 0 before the first.
        seq: u64,
        latest: Option<ID>,
        /// Earliest scheduled start announced that was in the future when
        /// announced; 0 when none. Devices treat a past value as due.
        next_start_ms: u64,
        /// Component name → its latest announced deployment.
        components: VecMap<String, ID>,
    }

    public struct DeviceInbox has key {
        id: UID,
        device_id: ID,
        version: u64,
        head: Head,
        /// Groups the device was added to through `add_member`/`follow_group`.
        /// A hint: devices check membership on the group itself.
        groups: vector<ID>,
    }

    // ── Events ────────────────────────────────────────────────────────────────

    public struct InboxCreated has copy, drop {
        inbox_id: ID,
        device_id: ID,
    }

    public struct Announced has copy, drop {
        /// The group or the device inbox.
        target: ID,
        deployment_id: ID,
        seq: u64,
        next_start_ms: u64,
    }

    // ── Inboxes ───────────────────────────────────────────────────────────────

    /// Create the device's inbox at its derived address. Its operator only;
    /// once per device.
    public fun create_inbox(registry: &mut DeviceRegistry, cap: &DeviceOwnerCap) {
        share_inbox(new_inbox(registry, cap));
    }

    /// As `create_inbox`, but returns the inbox so the same transaction can
    /// announce to it or add the device to a group before `share_inbox`.
    public fun new_inbox(registry: &mut DeviceRegistry, cap: &DeviceOwnerCap): DeviceInbox {
        let device_id = device_registry::owner_cap_device_id(cap);
        p1(registry, cap, device_id);
        let id = derived_object::claim(device_registry::uid_mut(registry), InboxKey { device_id });
        let inbox = DeviceInbox { id, device_id, version: INBOX_VERSION, head: p0(), groups: vector[] };
        event::emit(InboxCreated { inbox_id: object::id(&inbox), device_id });
        inbox
    }

    public fun share_inbox(inbox: DeviceInbox) {
        transfer::share_object(inbox);
    }

    public fun inbox_exists(registry: &DeviceRegistry, device_id: ID): bool {
        derived_object::exists(device_registry::uid(registry), InboxKey { device_id })
    }

    /// The device's inbox address, whether or not it was created yet.
    public fun inbox_address(registry_id: ID, device_id: ID): address {
        derived_object::derive_address(registry_id, InboxKey { device_id })
    }

    // ── Announcements ─────────────────────────────────────────────────────────

    /// Announce `deployment` to one of its target groups. The creator must be
    /// the group's admin, as devices require.
    public fun announce_group(group: &mut DeviceGroup, deployment: &DeploymentRecord, clock: &Clock, ctx: &mut TxContext) {
        let creator = deployment_manager::creator(deployment);
        assert!(ctx.sender() == creator, ENotCreator);
        assert!(device_group::admin(group) == creator, ENotAdmin);
        let group_id = device_group::group_id(group);
        assert!(deployment_manager::target_groups(deployment).contains(&group_id), ENotTargeted);
        let uid = device_group::uid_mut(group);
        if (!df::exists(uid, HeadKey {})) {
            df::add(uid, HeadKey {}, p0());
        };
        let head: &mut Head = df::borrow_mut(uid, HeadKey {});
        p2(head, group_id, deployment, clock);
    }

    /// Announce `deployment` to one of its target devices. The creator must be
    /// the device's current operator, proven by its DeviceOwnerCap.
    public fun announce_device(
        inbox: &mut DeviceInbox,
        registry: &DeviceRegistry,
        cap: &DeviceOwnerCap,
        deployment: &DeploymentRecord,
        clock: &Clock,
        ctx: &mut TxContext,
    ) {
        assert!(ctx.sender() == deployment_manager::creator(deployment), ENotCreator);
        let device_id = inbox.device_id;
        p6(device_registry::owner_cap_device_id(cap) == device_id);
        p1(registry, cap, device_id);
        assert!(deployment_manager::target_devices(deployment).contains(&device_id), ENotTargeted);
        let target = object::id(inbox);
        p2(&mut inbox.head, target, deployment, clock);
    }

    // ── Group membership ──────────────────────────────────────────────────────

    /// `device_group::add_device`, also listing the group in the device's inbox.
    public fun add_member(group: &mut DeviceGroup, inbox: &mut DeviceInbox, device: &Device, ctx: &mut TxContext) {
        p6(device_registry::device_id(device) == inbox.device_id);
        device_group::add_device(group, device, ctx);
        p3(inbox, device_group::group_id(group));
    }

    /// `device_group::remove_device`, also unlisting the group.
    public fun remove_member(group: &mut DeviceGroup, inbox: &mut DeviceInbox, ctx: &mut TxContext) {
        device_group::remove_device(group, inbox.device_id, ctx);
        p4(inbox, device_group::group_id(group));
    }

    /// List a group the device already belongs to (members added before
    /// inboxes, or through `device_group::add_device`). Permissionless: it
    /// only records verified membership.
    public fun follow_group(inbox: &mut DeviceInbox, group: &DeviceGroup) {
        assert!(device_group::contains_device(group, inbox.device_id), ENotMember);
        p3(inbox, device_group::group_id(group));
    }

    /// Unlist a group (for example a deleted one). The device's operator only.
    public fun forget_group(inbox: &mut DeviceInbox, registry: &DeviceRegistry, cap: &DeviceOwnerCap, group_id: ID) {
        p6(device_registry::owner_cap_device_id(cap) == inbox.device_id);
        p1(registry, cap, inbox.device_id);
        p4(inbox, group_id);
    }

    // ── Reads ─────────────────────────────────────────────────────────────────

    /// An empty head before the group's first announcement.
    public fun group_head(group: &DeviceGroup): Head {
        let uid = device_group::uid(group);
        if (df::exists(uid, HeadKey {})) *df::borrow(uid, HeadKey {}) else p0()
    }

    public fun inbox_head(inbox: &DeviceInbox): Head { inbox.head }
    public fun inbox_device_id(inbox: &DeviceInbox): ID { inbox.device_id }
    public fun inbox_groups(inbox: &DeviceInbox): &vector<ID> { &inbox.groups }
    public fun seq(head: &Head): u64 { head.seq }
    public fun latest(head: &Head): Option<ID> { head.latest }
    public fun next_start_ms(head: &Head): u64 { head.next_start_ms }
    public fun components(head: &Head): VecMap<String, ID> { head.components }

    // ── Internals ─────────────────────────────────────────────────────────────

    /// empty_head: compact private symbol to bound published bytecode storage.
    fun p0(): Head {
        Head { seq: (SMALL_0 as u64), latest: option::none(), next_start_ms: (SMALL_0 as u64), components: vec_map::empty() }
    }

    /// The cap must be the one the registry's identity names and the device
    /// must still be in custody: a stale or deregistered device's cap fails.
    /// assert_operator: compact private symbol to bound published bytecode storage.
    fun p1(registry: &DeviceRegistry, cap: &DeviceOwnerCap, device_id: ID) {
        let identity = device_registry::identity(registry, device_id);
        p5(identity.is_some());
        let identity = identity.destroy_some();
        p5(device_registry::is_custodied(registry, device_id));
        p5(device_registry::identity_owner_cap(&identity) == object::id(cap));
    }

    /// bump: compact private symbol to bound published bytecode storage.
    fun p2(head: &mut Head, target: ID, deployment: &DeploymentRecord, clock: &Clock) {
        let deployment_id = object::id(deployment);
        head.seq = head.seq + (SMALL_1 as u64);
        head.latest = option::some(deployment_id);
        let name = *deployment_manager::component_name(deployment);
        if (head.components.contains(&name)) {
            *head.components.get_mut(&name) = deployment_id;
        } else {
            if (head.components.length() >= MAX_COMPONENTS) {
                let (_, _) = head.components.remove_entry_by_idx((SMALL_0 as u64));
            };
            head.components.insert(name, deployment_id);
        };
        let now = clock.timestamp_ms();
        let start = deployment_manager::scheduled_start_ms(deployment);
        if (start > now && (head.next_start_ms <= now || start < head.next_start_ms)) {
            head.next_start_ms = start;
        };
        event::emit(Announced { target, deployment_id, seq: head.seq, next_start_ms: head.next_start_ms });
    }

    /// list_group: compact private symbol to bound published bytecode storage.
    fun p3(inbox: &mut DeviceInbox, group_id: ID) {
        if (!inbox.groups.contains(&group_id)) {
            assert!(inbox.groups.length() < MAX_GROUPS, ETooManyGroups);
            inbox.groups.push_back(group_id);
        };
    }

    /// unlist_group: compact private symbol to bound published bytecode storage.
    fun p4(inbox: &mut DeviceInbox, group_id: ID) {
        let (found, i) = inbox.groups.index_of(&group_id);
        if (found) { inbox.groups.remove(i); };
    }
    // Share each named abort branch without changing its module or error code.
    /// check_NotOperator: compact private symbol to bound published bytecode storage.
    fun p5(condition: bool) {
        if (!condition) abort ENotOperator
    }
    /// check_WrongInbox: compact private symbol to bound published bytecode storage.
    fun p6(condition: bool) {
        if (!condition) abort EWrongInbox
    }
}
