module sui_edge::deployment_manager {
    use std::string::String;
    use sui::clock::Clock;
    use sui::dynamic_field as df;
    use sui::event;
    use sui::vec_map::{Self, VecMap};
    use sui_edge::device_cap::{Self, DeviceCap};
    use sui_edge::device_registry::DeviceRegistry;
    use sui_edge::device_group::{Self, DeviceGroup};

    // ── Constants ─────────────────────────────────────────────────────────────

    const SCHEMA_VERSION: u64 = 1;
    /// Version of the authoritative receipt state written by this package.
    /// A future upgrade that changes report authority must bump this so that
    /// this package's (still callable) entry points abort on migrated records.
    /// Version 2: reports require an active registry identity (revocation,
    /// rotation, deregistration). Locks the version-1 package out of new and
    /// migrated records. Statuses written under an older version are discarded
    /// on migration and never returned as receipts: an older package may have
    /// accepted them from a since-revoked key.
    const RECEIPTS_VERSION: u64 = 2;
    const STATUS_APPLIED: u8 = 1;
    const STATUS_FAILED: u8 = 2;

    // ── Errors ────────────────────────────────────────────────────────────────

    const EInvalidStatus: u64 = 0;
    const ENoTargets: u64 = 1;
    const ENotCreator: u64 = 2;
    const ENotTargeted: u64 = 3;
    const EInvalidRecipeHash: u64 = 4;
    const EPackageOutdated: u64 = 5;
    const ENoReceipts: u64 = 6;
    const EUseVerifiedReport: u64 = 7;
    const EStaleReceipts: u64 = 8;

    // ── Dynamic field keys ────────────────────────────────────────────────────
    //
    // Typed key structs ensure compile-time safety and prevent key collisions.
    // New fields can be added in future upgrades without touching DeploymentRecord.

    public struct TargetGroupsKey has copy, drop, store {}
    public struct TargetDevicesKey has copy, drop, store {}
    public struct StatusesKey has copy, drop, store {}
    public struct RecipeDigestKey has copy, drop, store {}
    public struct ReceiptsKey has copy, drop, store {}

    /// Authoritative per-device status receipts.
    ///
    /// Older package versions stay callable after an upgrade and cannot be
    /// disabled. They can only touch types they define, so trusted state lives
    /// under a key type introduced by the upgrade, never under `StatusesKey`.
    /// `version` lets a later upgrade lock out this package's entry points.
    public struct Receipts has store {
        version: u64,
        statuses: VecMap<ID, u8>,
    }

    // ── Objects ───────────────────────────────────────────────────────────────

    /// Shared deployment record. Core identity fields are stable; all mutable
    /// and extensible data lives in typed dynamic fields.
    public struct DeploymentRecord has key {
        id: UID,
        creator: address,
        component_name: String,
        version: String,
        /// Walrus blob ID of the component recipe JSON (which embeds the artifact blob ID).
        recipe_blob_id: String,
        /// SHA-256 of the raw artifact binary — used by the edge device to verify integrity.
        artifact_sha256: String,
        created_at: u64,
        /// Monotonic version bump used to gate future migrations.
        schema_version: u64,
        // Dynamic fields attached at creation:
        //   TargetGroupsKey  → vector<ID>          DeviceGroup object IDs
        //   TargetDevicesKey → vector<ID>          Individual Device object IDs
        //   ReceiptsKey      → Receipts            authoritative, signer-checked statuses
        //   RecipeDigestKey  → String              recipe SHA-256 (verified creation only)
        // Records created by the original package also carry:
        //   StatusesKey      → VecMap<ID, u8>      legacy, unauthenticated statuses;
        //                                          old entry points can still write it
    }

    // ── Events ────────────────────────────────────────────────────────────────

    public struct DeploymentCreated has copy, drop {
        deployment_id: ID,
        component_name: String,
        version: String,
        recipe_blob_id: String,
        artifact_sha256: String,
        target_groups: vector<ID>,
        target_devices: vector<ID>,
        creator: address,
    }

    /// Legacy event. Its type originates in the first package, so old entry
    /// points can still emit it for any device without authorization.
    public struct DeviceStatusUpdated has copy, drop {
        deployment_id: ID,
        device_id: ID,
        status: u8,
    }

    /// Status event, emitted after this package's identity and target checks.
    /// Packages from receipts version 1 also emit it under signer-only checks
    /// (even for a since-revoked key), so it is a notification, not evidence:
    /// trust current-version receipt state (`has_current_receipts`).
    public struct DeviceStatusReported has copy, drop {
        deployment_id: ID,
        device_id: ID,
        status: u8,
        reporter: address,
    }

    public struct DeploymentDeleted has copy, drop {
        deployment_id: ID,
        creator: address,
    }

    // ── Public functions ──────────────────────────────────────────────────────

    /// Create a deployment targeting device groups, individual devices, or both.
    public fun create_deployment(
        component_name: String,
        version: String,
        recipe_blob_id: String,
        artifact_sha256: String,
        target_groups: vector<ID>,
        target_devices: vector<ID>,
        clock: &Clock,
        ctx: &mut TxContext,
    ) {
        let record = make_deployment(component_name, version, recipe_blob_id, artifact_sha256,
            target_groups, target_devices, clock, ctx);
        transfer::share_object(record);
    }

    /// New clients authenticate the exact recipe bytes as well as the artifact.
    /// The original creation API is retained for compatible package upgrades.
    public fun create_deployment_verified(
        component_name: String,
        version: String,
        recipe_blob_id: String,
        artifact_sha256: String,
        recipe_sha256: String,
        target_groups: vector<ID>,
        target_devices: vector<ID>,
        clock: &Clock,
        ctx: &mut TxContext,
    ) {
        let bytes = recipe_sha256.as_bytes();
        assert!(bytes.length() == 64, EInvalidRecipeHash);
        let mut i = 0;
        while (i < bytes.length()) {
            let byte = bytes[i];
            assert!((byte >= 48 && byte <= 57) || (byte >= 97 && byte <= 102), EInvalidRecipeHash);
            i = i + 1;
        };
        let mut record = make_deployment(component_name, version, recipe_blob_id, artifact_sha256,
            target_groups, target_devices, clock, ctx);
        df::add(&mut record.id, RecipeDigestKey {}, recipe_sha256);
        transfer::share_object(record);
    }

    fun make_deployment(
        component_name: String,
        version: String,
        recipe_blob_id: String,
        artifact_sha256: String,
        target_groups: vector<ID>,
        target_devices: vector<ID>,
        clock: &Clock,
        ctx: &mut TxContext,
    ): DeploymentRecord {
        assert!(!target_groups.is_empty() || !target_devices.is_empty(), ENoTargets);

        let mut record = DeploymentRecord {
            id: object::new(ctx),
            creator: ctx.sender(),
            component_name,
            version,
            recipe_blob_id,
            artifact_sha256,
            created_at: clock.timestamp_ms(),
            schema_version: SCHEMA_VERSION,
        };

        event::emit(DeploymentCreated {
            deployment_id: object::id(&record),
            component_name: record.component_name,
            version: record.version,
            recipe_blob_id: record.recipe_blob_id,
            artifact_sha256: record.artifact_sha256,
            target_groups,
            target_devices,
            creator: record.creator,
        });

        df::add(&mut record.id, TargetGroupsKey {},  target_groups);
        df::add(&mut record.id, TargetDevicesKey {}, target_devices);
        // No legacy StatusesKey: old `report_status` aborts on new records.
        df::add(&mut record.id, ReceiptsKey {},      new_receipts());

        record
    }

    /// Superseded: this signature cannot see the registry, so it cannot tell a
    /// revoked or rotated device from an active one. Use `report_verified_status`.
    public fun report_status(
        _deployment: &mut DeploymentRecord,
        _cap: &DeviceCap,
        _status: u8,
        _ctx: &TxContext,
    ) {
        abort EUseVerifiedReport
    }

    /// Superseded: use `report_verified_status_for_group`.
    public fun report_status_for_group(
        _deployment: &mut DeploymentRecord,
        _cap: &DeviceCap,
        _group: &DeviceGroup,
        _status: u8,
        _ctx: &TxContext,
    ) {
        abort EUseVerifiedReport
    }

    /// Called by the edge device after applying (or failing to apply) a deployment.
    public fun report_verified_status(
        deployment: &mut DeploymentRecord,
        registry: &DeviceRegistry,
        cap: &DeviceCap,
        status: u8,
        ctx: &TxContext,
    ) {
        device_cap::assert_active(cap, registry, ctx);
        let device_id = device_cap::device_id(cap);
        let targets: &vector<ID> = df::borrow(&deployment.id, TargetDevicesKey {});
        assert!(targets.contains(&device_id), ENotTargeted);
        record_status(deployment, cap, status, ctx);
    }

    /// Group reports prove current membership in a group targeted by this deployment.
    public fun report_verified_status_for_group(
        deployment: &mut DeploymentRecord,
        registry: &DeviceRegistry,
        cap: &DeviceCap,
        group: &DeviceGroup,
        status: u8,
        ctx: &TxContext,
    ) {
        device_cap::assert_active(cap, registry, ctx);
        let targets: &vector<ID> = df::borrow(&deployment.id, TargetGroupsKey {});
        assert!(targets.contains(&device_group::group_id(group)), ENotTargeted);
        assert!(device_group::admin(group) == deployment.creator, ENotTargeted);
        assert!(device_group::contains_device(group, device_cap::device_id(cap)), ENotTargeted);
        record_status(deployment, cap, status, ctx);
    }

    /// Attach or upgrade authoritative receipts without recording a status.
    /// Permissionless and idempotent: it only discards statuses written under
    /// an older authority version, and it aborts rather than downgrade a record
    /// migrated by a newer package. After an authority upgrade, sweep live
    /// records with the new package's `migrate` so older entry points can no
    /// longer write them.
    public fun migrate(deployment: &mut DeploymentRecord) {
        receipts_mut(deployment);
    }

    fun record_status(deployment: &mut DeploymentRecord, cap: &DeviceCap, status: u8, ctx: &TxContext) {
        assert!(status == STATUS_APPLIED || status == STATUS_FAILED, EInvalidStatus);

        let device_id = device_cap::device_id(cap);
        // Legacy StatusesKey entries are never copied: old entry points may
        // have forged them. They stay on the record as unverified history.
        let statuses = &mut receipts_mut(deployment).statuses;

        if (statuses.contains(&device_id)) {
            *statuses.get_mut(&device_id) = status;
        } else {
            statuses.insert(device_id, status);
        };

        event::emit(DeviceStatusReported {
            deployment_id: object::id(deployment),
            device_id,
            status,
            reporter: ctx.sender(),
        });
    }

    fun new_receipts(): Receipts {
        Receipts { version: RECEIPTS_VERSION, statuses: vec_map::empty() }
    }

    /// Records created before receipts existed get them lazily; a record
    /// already migrated by a newer package aborts here. Statuses from an older
    /// authority version are dropped: devices must report again.
    fun receipts_mut(deployment: &mut DeploymentRecord): &mut Receipts {
        if (!df::exists(&deployment.id, ReceiptsKey {})) {
            df::add(&mut deployment.id, ReceiptsKey {}, new_receipts());
        };
        let receipts: &mut Receipts = df::borrow_mut(&mut deployment.id, ReceiptsKey {});
        assert!(receipts.version <= RECEIPTS_VERSION, EPackageOutdated);
        if (receipts.version < RECEIPTS_VERSION) {
            receipts.statuses = vec_map::empty();
            receipts.version = RECEIPTS_VERSION;
        };
        receipts
    }

    public entry fun delete_deployment(mut deployment: DeploymentRecord, ctx: &TxContext) {
        assert!(ctx.sender() == deployment.creator, ENotCreator);

        // Remove dynamic fields before destroying the object.
        let _: vector<ID>       = df::remove(&mut deployment.id, TargetGroupsKey {});
        let _: vector<ID>       = df::remove(&mut deployment.id, TargetDevicesKey {});
        if (df::exists(&deployment.id, StatusesKey {})) {
            let _: VecMap<ID, u8> = df::remove(&mut deployment.id, StatusesKey {});
        };
        if (df::exists(&deployment.id, ReceiptsKey {})) {
            let Receipts { version: _, statuses: _ } = df::remove(&mut deployment.id, ReceiptsKey {});
        };
        if (df::exists(&deployment.id, RecipeDigestKey {})) {
            let _: String = df::remove(&mut deployment.id, RecipeDigestKey {});
        };

        let DeploymentRecord { id, creator, component_name: _, version: _, recipe_blob_id: _, artifact_sha256: _, created_at: _, schema_version: _ } = deployment;
        event::emit(DeploymentDeleted { deployment_id: id.to_inner(), creator });
        id.delete();
    }

    // ── Read helpers ──────────────────────────────────────────────────────────

    public fun target_groups(d: &DeploymentRecord): &vector<ID> {
        df::borrow(&d.id, TargetGroupsKey {})
    }

    public fun target_devices(d: &DeploymentRecord): &vector<ID> {
        df::borrow(&d.id, TargetDevicesKey {})
    }

    /// Legacy, unauthenticated statuses. Old package versions can write these
    /// without any check, so never treat them as proof of a device report.
    /// Aborts for records created after receipts were introduced.
    public fun statuses(d: &DeploymentRecord): &VecMap<ID, u8> {
        df::borrow(&d.id, StatusesKey {})
    }

    public fun has_receipts(d: &DeploymentRecord): bool {
        df::exists(&d.id, ReceiptsKey {})
    }

    /// Receipts written under this package's report authority. Older versions
    /// (still writable by older packages until migrated) are not evidence.
    public fun has_current_receipts(d: &DeploymentRecord): bool {
        has_receipts(d) && receipts_version(d) >= RECEIPTS_VERSION
    }

    /// Authoritative statuses: only reports from an active device identity.
    public fun receipt_statuses(d: &DeploymentRecord): &VecMap<ID, u8> {
        assert!(has_receipts(d), ENoReceipts);
        assert!(has_current_receipts(d), EStaleReceipts);
        let receipts: &Receipts = df::borrow(&d.id, ReceiptsKey {});
        &receipts.statuses
    }

    public fun receipt_status(d: &DeploymentRecord, device_id: ID): Option<u8> {
        if (!has_current_receipts(d)) return option::none();
        let statuses = receipt_statuses(d);
        if (statuses.contains(&device_id)) option::some(*statuses.get(&device_id))
        else option::none()
    }

    public fun receipts_version(d: &DeploymentRecord): u64 {
        assert!(has_receipts(d), ENoReceipts);
        let receipts: &Receipts = df::borrow(&d.id, ReceiptsKey {});
        receipts.version
    }

    /// Records created by the original package, before receipts existed.
    #[test_only]
    public fun create_legacy_for_testing(target_devices: vector<ID>, ctx: &mut TxContext) {
        let mut record = DeploymentRecord {
            id: object::new(ctx),
            creator: ctx.sender(),
            component_name: std::string::utf8(b"legacy"),
            version: std::string::utf8(b"0"),
            recipe_blob_id: std::string::utf8(b"blob"),
            artifact_sha256: std::string::utf8(b"sha"),
            created_at: 0,
            schema_version: SCHEMA_VERSION,
        };
        df::add(&mut record.id, TargetGroupsKey {},  vector<ID>[]);
        df::add(&mut record.id, TargetDevicesKey {}, target_devices);
        df::add(&mut record.id, StatusesKey {},      vec_map::empty<ID, u8>());
        transfer::share_object(record);
    }

    /// Replays the original package's unchecked `report_status` body, which
    /// remains callable on chain after any upgrade.
    #[test_only]
    public fun legacy_report_for_testing(deployment: &mut DeploymentRecord, device_id: ID, status: u8) {
        let statuses: &mut VecMap<ID, u8> = df::borrow_mut(&mut deployment.id, StatusesKey {});
        if (statuses.contains(&device_id)) {
            *statuses.get_mut(&device_id) = status;
        } else {
            statuses.insert(device_id, status);
        };
        event::emit(DeviceStatusUpdated { deployment_id: object::id(deployment), device_id, status });
    }

    /// Replays an older package's signer-only report on a record it can still
    /// write (receipts at an older version).
    #[test_only]
    public fun old_package_report_for_testing(d: &mut DeploymentRecord, device_id: ID, status: u8) {
        let receipts: &mut Receipts = df::borrow_mut(&mut d.id, ReceiptsKey {});
        assert!(receipts.version < RECEIPTS_VERSION, EPackageOutdated);
        if (receipts.statuses.contains(&device_id)) {
            *receipts.statuses.get_mut(&device_id) = status;
        } else {
            receipts.statuses.insert(device_id, status);
        };
    }

    /// Simulates a newer package having migrated this record's receipts.
    #[test_only]
    public fun set_receipts_version_for_testing(d: &mut DeploymentRecord, version: u64) {
        let receipts: &mut Receipts = df::borrow_mut(&mut d.id, ReceiptsKey {});
        receipts.version = version;
    }

    public fun recipe_sha256(d: &DeploymentRecord): &String {
        df::borrow(&d.id, RecipeDigestKey {})
    }

    public fun recipe_blob_id(d: &DeploymentRecord): &String { &d.recipe_blob_id }
    public fun artifact_sha256(d: &DeploymentRecord): &String { &d.artifact_sha256 }
    public fun status_applied(): u8 { STATUS_APPLIED }
    public fun status_failed(): u8 { STATUS_FAILED }
}
