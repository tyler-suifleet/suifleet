/// Seal decryption policies for confidential releases (SF-022).
///
/// An `AccessPolicy` is a shared object naming who may obtain Seal
/// decryption keys for content encrypted under its identities: accounts by
/// address, devices by registry ID (only while the device's active,
/// unrevoked key asks), and device groups (devices currently in the group,
/// likewise active). Holding its `AccessAdminCap` authorizes grants,
/// revocations and key-epoch rotation. Decryption access authorizes nothing
/// else: publishing, deploying and administration stay with their own
/// capabilities and roles.
///
/// Seal identity (the key ID without Seal's package prefix):
///   policy object ID (32 bytes) || key_epoch (u64, little-endian) || nonce
/// with a random nonce of 16 to 64 bytes per release. Seal namespaces key IDs
/// by this package's original ID, and key servers accept `seal_approve*`
/// calls through any version of the package. So every approval checks the
/// policy's `version` against this code's `VERSION`: after an upgrade bumps
/// `VERSION` and `migrate` moves the policy, older code always refuses it.
///
/// `rotate_key_epoch` makes every identity of an earlier epoch unapprovable;
/// new content must be encrypted under the current epoch. Neither rotation
/// nor revocation recalls a key or plaintext a recipient already obtained,
/// and key servers observe chain state asynchronously.
///
/// Introduced by a package upgrade: older package versions cannot reach it.
module sui_edge::seal_policy {
    use std::string::String;
    use sui::bcs;
    use sui::event;
    use sui_edge::device_group::{Self, DeviceGroup};
    use sui_edge::device_registry::{Self, DeviceRegistry};

    // ── Constants ─────────────────────────────────────────────────────────────

    /// Policy code version; approvals require `AccessPolicy.version == VERSION`.
    const VERSION: u64 = 1;
    /// Bytes of policy ID and key epoch before the nonce.
    const PREFIX_LENGTH: u64 = 40;
    const MIN_NONCE: u64 = 16;
    const MAX_NONCE: u64 = 64;
    /// Bound on each scope list, which approvals scan.
    const MAX_ENTRIES: u64 = 256;

    const SCOPE_ACCOUNT: u8 = 0;
    const SCOPE_DEVICE: u8 = 1;
    const SCOPE_GROUP: u8 = 2;

    // ── Errors ────────────────────────────────────────────────────────────────

    const ENoAccess: u64 = 0;
    const EWrongCap: u64 = 1;
    const EWrongVersion: u64 = 2;
    const EAlreadyGranted: u64 = 3;
    const ENotGranted: u64 = 4;
    const ETooMany: u64 = 5;
    const ENotGroupAdmin: u64 = 6;
    const EUnknownDevice: u64 = 7;
    const ENotNewer: u64 = 8;

    // ── Objects ───────────────────────────────────────────────────────────────

    public struct AccessPolicy has key {
        id: UID,
        /// Code version this policy accepts approvals from.
        version: u64,
        name: String,
        /// Identities must carry exactly this epoch.
        key_epoch: u64,
        accounts: vector<address>,
        devices: vector<ID>,
        groups: vector<ID>,
    }

    /// Administers one policy. Transferable: whoever holds it administers
    /// the policy; it grants nothing over devices, groups or deployments.
    public struct AccessAdminCap has key, store {
        id: UID,
        policy_id: ID,
    }

    // ── Events ────────────────────────────────────────────────────────────────

    public struct PolicyCreated has copy, drop {
        policy_id: ID,
        cap_id: ID,
        name: String,
        creator: address,
    }

    /// `scope`: 0 account, 1 device, 2 group; `subject` is the address or ID.
    public struct AccessChanged has copy, drop {
        policy_id: ID,
        scope: u8,
        subject: address,
        granted: bool,
    }

    public struct KeyEpochRotated has copy, drop {
        policy_id: ID,
        key_epoch: u64,
    }

    public struct PolicyMigrated has copy, drop {
        policy_id: ID,
        version: u64,
    }

    // ── Administration ────────────────────────────────────────────────────────

    /// Share a new, empty policy; returns its admin cap.
    public fun create_policy(name: String, ctx: &mut TxContext): AccessAdminCap {
        let policy = AccessPolicy {
            id: object::new(ctx),
            version: VERSION,
            name,
            key_epoch: 0,
            accounts: vector[],
            devices: vector[],
            groups: vector[],
        };
        let policy_id = object::id(&policy);
        let cap = AccessAdminCap { id: object::new(ctx), policy_id };
        event::emit(PolicyCreated { policy_id, cap_id: object::id(&cap), name: policy.name, creator: ctx.sender() });
        transfer::share_object(policy);
        cap
    }

    public fun grant_account(policy: &mut AccessPolicy, cap: &AccessAdminCap, account: address) {
        check_admin(policy, cap);
        add(&mut policy.accounts, account);
        changed(policy, SCOPE_ACCOUNT, account, true);
    }

    public fun revoke_account(policy: &mut AccessPolicy, cap: &AccessAdminCap, account: address) {
        check_admin(policy, cap);
        remove(&mut policy.accounts, account);
        changed(policy, SCOPE_ACCOUNT, account, false);
    }

    /// The device must be activated (in registry custody). Approvals further
    /// require its current, unrevoked key.
    public fun grant_device(policy: &mut AccessPolicy, cap: &AccessAdminCap, registry: &DeviceRegistry, device_id: ID) {
        check_admin(policy, cap);
        assert!(device_registry::is_custodied(registry, device_id), EUnknownDevice);
        add(&mut policy.devices, device_id);
        changed(policy, SCOPE_DEVICE, device_id.to_address(), true);
    }

    public fun revoke_device(policy: &mut AccessPolicy, cap: &AccessAdminCap, device_id: ID) {
        check_admin(policy, cap);
        remove(&mut policy.devices, device_id);
        changed(policy, SCOPE_DEVICE, device_id.to_address(), false);
    }

    /// Only a group's own admin may grant it: the group's admin decides its
    /// membership, and so which devices the grant covers.
    public fun grant_group(policy: &mut AccessPolicy, cap: &AccessAdminCap, group: &DeviceGroup, ctx: &TxContext) {
        check_admin(policy, cap);
        assert!(device_group::admin(group) == ctx.sender(), ENotGroupAdmin);
        let group_id = device_group::group_id(group);
        add(&mut policy.groups, group_id);
        changed(policy, SCOPE_GROUP, group_id.to_address(), true);
    }

    /// Takes the ID, so a deleted group can still be revoked.
    public fun revoke_group(policy: &mut AccessPolicy, cap: &AccessAdminCap, group_id: ID) {
        check_admin(policy, cap);
        remove(&mut policy.groups, group_id);
        changed(policy, SCOPE_GROUP, group_id.to_address(), false);
    }

    /// Retire every identity of the current epoch.
    public fun rotate_key_epoch(policy: &mut AccessPolicy, cap: &AccessAdminCap) {
        check_admin(policy, cap);
        policy.key_epoch = policy.key_epoch + 1;
        event::emit(KeyEpochRotated { policy_id: object::id(policy), key_epoch: policy.key_epoch });
    }

    /// Move the policy to this code's version; older code then refuses it.
    public fun migrate(policy: &mut AccessPolicy, cap: &AccessAdminCap) {
        assert!(cap.policy_id == object::id(policy), EWrongCap);
        assert!(policy.version < VERSION, ENotNewer);
        policy.version = VERSION;
        event::emit(PolicyMigrated { policy_id: object::id(policy), version: VERSION });
    }

    // ── Seal approvals (evaluated by key servers; side-effect free) ───────────

    /// An account granted by address.
    entry fun seal_approve_account(id: vector<u8>, policy: &AccessPolicy, ctx: &TxContext) {
        assert!(account_allowed(policy, &id, ctx.sender()), ENoAccess);
    }

    /// A device granted directly, asking with its active key.
    entry fun seal_approve_device(id: vector<u8>, policy: &AccessPolicy, registry: &DeviceRegistry, device_id: ID, ctx: &TxContext) {
        assert!(device_allowed(policy, &id, registry, device_id, ctx.sender()), ENoAccess);
    }

    /// A device in a granted group, asking with its active key.
    entry fun seal_approve_group_device(
        id: vector<u8>,
        policy: &AccessPolicy,
        registry: &DeviceRegistry,
        group: &DeviceGroup,
        device_id: ID,
        ctx: &TxContext,
    ) {
        assert!(group_device_allowed(policy, &id, registry, group, device_id, ctx.sender()), ENoAccess);
    }

    public fun account_allowed(policy: &AccessPolicy, id: &vector<u8>, sender: address): bool {
        identity_matches(policy, id) && policy.accounts.contains(&sender)
    }

    public fun device_allowed(policy: &AccessPolicy, id: &vector<u8>, registry: &DeviceRegistry, device_id: ID, sender: address): bool {
        identity_matches(policy, id)
            && policy.devices.contains(&device_id)
            && device_registry::is_active_signer(registry, device_id, sender)
    }

    public fun group_device_allowed(
        policy: &AccessPolicy,
        id: &vector<u8>,
        registry: &DeviceRegistry,
        group: &DeviceGroup,
        device_id: ID,
        sender: address,
    ): bool {
        identity_matches(policy, id)
            && policy.groups.contains(&device_group::group_id(group))
            && device_group::contains_device(group, device_id)
            && device_registry::is_active_signer(registry, device_id, sender)
    }

    // ── Reads ─────────────────────────────────────────────────────────────────

    /// The identity prefix new content must use: policy ID || key epoch.
    public fun identity_prefix(policy: &AccessPolicy): vector<u8> {
        let mut prefix = object::id(policy).to_bytes();
        prefix.append(bcs::to_bytes(&policy.key_epoch));
        prefix
    }

    /// Whether `id` is a current identity of this policy, under this code version.
    public fun identity_matches(policy: &AccessPolicy, id: &vector<u8>): bool {
        assert!(policy.version == VERSION, EWrongVersion);
        let length = id.length();
        if (length < PREFIX_LENGTH + MIN_NONCE || length > PREFIX_LENGTH + MAX_NONCE) return false;
        let prefix = identity_prefix(policy);
        let mut i = 0;
        while (i < PREFIX_LENGTH) {
            if (prefix[i] != id[i]) return false;
            i = i + 1;
        };
        true
    }

    public fun version(policy: &AccessPolicy): u64 { policy.version }
    public fun name(policy: &AccessPolicy): String { policy.name }
    public fun key_epoch(policy: &AccessPolicy): u64 { policy.key_epoch }
    public fun accounts(policy: &AccessPolicy): &vector<address> { &policy.accounts }
    public fun devices(policy: &AccessPolicy): &vector<ID> { &policy.devices }
    public fun groups(policy: &AccessPolicy): &vector<ID> { &policy.groups }
    public fun cap_policy_id(cap: &AccessAdminCap): ID { cap.policy_id }
    public fun code_version(): u64 { VERSION }

    // ── Internal ──────────────────────────────────────────────────────────────

    fun check_admin(policy: &AccessPolicy, cap: &AccessAdminCap) {
        assert!(cap.policy_id == object::id(policy), EWrongCap);
        assert!(policy.version == VERSION, EWrongVersion);
    }

    fun add<T: copy + drop>(list: &mut vector<T>, item: T) {
        assert!(!list.contains(&item), EAlreadyGranted);
        assert!(list.length() < MAX_ENTRIES, ETooMany);
        list.push_back(item);
    }

    fun remove<T: copy + drop>(list: &mut vector<T>, item: T) {
        let (found, index) = list.index_of(&item);
        assert!(found, ENotGranted);
        list.swap_remove(index);
    }

    fun changed(policy: &AccessPolicy, scope: u8, subject: address, granted: bool) {
        event::emit(AccessChanged { policy_id: object::id(policy), scope, subject, granted });
    }

    #[test_only]
    public fun seal_approve_device_for_testing(id: vector<u8>, policy: &AccessPolicy, registry: &DeviceRegistry, device_id: ID, ctx: &TxContext) {
        seal_approve_device(id, policy, registry, device_id, ctx)
    }

    #[test_only]
    public fun set_version_for_testing(policy: &mut AccessPolicy, version: u64) { policy.version = version }
}
