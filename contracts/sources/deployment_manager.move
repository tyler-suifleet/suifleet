module sui_edge::deployment_manager {
    use std::string::String;
    use sui::clock::Clock;
    use sui::dynamic_field as df;
    use sui::event;
    use sui::vec_map::{Self, VecMap};
    use sui_edge::device_cap::{Self, DeviceCap};
    use sui_edge::device_group::{Self, DeviceGroup};

    // ── Constants ─────────────────────────────────────────────────────────────

    const SCHEMA_VERSION: u64 = 1;
    const STATUS_APPLIED: u8 = 1;
    const STATUS_FAILED: u8 = 2;

    // ── Errors ────────────────────────────────────────────────────────────────

    const EInvalidStatus: u64 = 0;
    const ENoTargets: u64 = 1;
    const ENotCreator: u64 = 2;
    const ENotTargeted: u64 = 3;
    const EInvalidRecipeHash: u64 = 4;

    // ── Dynamic field keys ────────────────────────────────────────────────────
    //
    // Typed key structs ensure compile-time safety and prevent key collisions.
    // New fields can be added in future upgrades without touching DeploymentRecord.

    public struct TargetGroupsKey has copy, drop, store {}
    public struct TargetDevicesKey has copy, drop, store {}
    public struct StatusesKey has copy, drop, store {}
    public struct RecipeDigestKey has copy, drop, store {}

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
        //   StatusesKey      → VecMap<ID, u8>      device_id → status (1=applied, 2=failed)
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

    public struct DeviceStatusUpdated has copy, drop {
        deployment_id: ID,
        device_id: ID,
        status: u8,
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
        df::add(&mut record.id, StatusesKey {},      vec_map::empty<ID, u8>());

        record
    }

    /// Called by the edge device after applying (or failing to apply) a deployment.
    public fun report_status(
        deployment: &mut DeploymentRecord,
        cap: &DeviceCap,
        status: u8,
        ctx: &TxContext,
    ) {
        device_cap::assert_signer(cap, ctx);
        let device_id = device_cap::device_id(cap);
        let targets: &vector<ID> = df::borrow(&deployment.id, TargetDevicesKey {});
        assert!(targets.contains(&device_id), ENotTargeted);
        record_status(deployment, cap, status);
    }

    /// Group reports prove current membership in a group targeted by this deployment.
    public fun report_status_for_group(
        deployment: &mut DeploymentRecord,
        cap: &DeviceCap,
        group: &DeviceGroup,
        status: u8,
        ctx: &TxContext,
    ) {
        device_cap::assert_signer(cap, ctx);
        let targets: &vector<ID> = df::borrow(&deployment.id, TargetGroupsKey {});
        assert!(targets.contains(&device_group::group_id(group)), ENotTargeted);
        assert!(device_group::admin(group) == deployment.creator, ENotTargeted);
        assert!(device_group::contains_device(group, device_cap::device_id(cap)), ENotTargeted);
        record_status(deployment, cap, status);
    }

    fun record_status(deployment: &mut DeploymentRecord, cap: &DeviceCap, status: u8) {
        assert!(status == STATUS_APPLIED || status == STATUS_FAILED, EInvalidStatus);

        let device_id = device_cap::device_id(cap);
        let statuses: &mut VecMap<ID, u8> = df::borrow_mut(&mut deployment.id, StatusesKey {});

        if (statuses.contains(&device_id)) {
            *statuses.get_mut(&device_id) = status;
        } else {
            statuses.insert(device_id, status);
        };

        event::emit(DeviceStatusUpdated {
            deployment_id: object::id(deployment),
            device_id,
            status,
        });
    }

    public entry fun delete_deployment(mut deployment: DeploymentRecord, ctx: &TxContext) {
        assert!(ctx.sender() == deployment.creator, ENotCreator);

        // Remove dynamic fields before destroying the object.
        let _: vector<ID>       = df::remove(&mut deployment.id, TargetGroupsKey {});
        let _: vector<ID>       = df::remove(&mut deployment.id, TargetDevicesKey {});
        let _: VecMap<ID, u8>   = df::remove(&mut deployment.id, StatusesKey {});
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

    public fun statuses(d: &DeploymentRecord): &VecMap<ID, u8> {
        df::borrow(&d.id, StatusesKey {})
    }

    public fun recipe_sha256(d: &DeploymentRecord): &String {
        df::borrow(&d.id, RecipeDigestKey {})
    }

    public fun recipe_blob_id(d: &DeploymentRecord): &String { &d.recipe_blob_id }
    public fun artifact_sha256(d: &DeploymentRecord): &String { &d.artifact_sha256 }
    public fun status_applied(): u8 { STATUS_APPLIED }
    public fun status_failed(): u8 { STATUS_FAILED }
}
