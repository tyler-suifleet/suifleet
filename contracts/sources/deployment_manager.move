module sui_edge::deployment_manager {
    use std::string::String;
    use sui::clock::Clock;
    use sui::event;
    use sui::vec_map::{Self, VecMap};
    use sui_edge::device_cap::{Self, DeviceCap};

    // ── Constants ─────────────────────────────────────────────────────────────

    const STATUS_APPLIED: u8 = 1;
    const STATUS_FAILED: u8 = 2;

    // ── Errors ────────────────────────────────────────────────────────────────

    const EInvalidStatus: u64 = 0;
    const ENoTargets: u64 = 1;
    const ENotCreator: u64 = 2;

    // ── Objects ───────────────────────────────────────────────────────────────

    /// Shared deployment record. Can target device groups, individual devices, or both.
    /// Per-device statuses are populated dynamically as devices report.
    public struct DeploymentRecord has key {
        id: UID,
        creator: address,
        firmware_name: String,
        version: String,
        walrus_blob_id: String,
        sha256_hash: String,
        /// DeviceGroup object IDs — all members of these groups receive this deployment.
        target_groups: vector<ID>,
        /// Individual Device object IDs targeted directly.
        target_devices: vector<ID>,
        /// Maps device_id → status (populated as devices report; 1=applied, 2=failed).
        statuses: VecMap<ID, u8>,
        created_at: u64,
    }

    // ── Events ────────────────────────────────────────────────────────────────

    public struct DeploymentCreated has copy, drop {
        deployment_id: ID,
        firmware_name: String,
        version: String,
        walrus_blob_id: String,
        sha256_hash: String,
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
        firmware_name: String,
        version: String,
        walrus_blob_id: String,
        sha256_hash: String,
        target_groups: vector<ID>,
        target_devices: vector<ID>,
        clock: &Clock,
        ctx: &mut TxContext,
    ) {
        assert!(!target_groups.is_empty() || !target_devices.is_empty(), ENoTargets);

        let record = DeploymentRecord {
            id: object::new(ctx),
            creator: ctx.sender(),
            firmware_name,
            version,
            walrus_blob_id,
            sha256_hash,
            target_groups,
            target_devices,
            statuses: vec_map::empty(),
            created_at: clock.timestamp_ms(),
        };

        event::emit(DeploymentCreated {
            deployment_id: object::id(&record),
            firmware_name: record.firmware_name,
            version: record.version,
            walrus_blob_id: record.walrus_blob_id,
            sha256_hash: record.sha256_hash,
            target_groups: record.target_groups,
            target_devices: record.target_devices,
            creator: record.creator,
        });

        transfer::share_object(record);
    }

    /// Called by the edge device after applying (or failing to apply) a deployment.
    /// Any device holding a valid DeviceCap may report; targeting is enforced by
    /// the edge client (which only processes deployments for its groups/device list).
    public fun report_status(
        deployment: &mut DeploymentRecord,
        cap: &DeviceCap,
        status: u8,
        _ctx: &TxContext,
    ) {
        assert!(status == STATUS_APPLIED || status == STATUS_FAILED, EInvalidStatus);

        let device_id = device_cap::device_id(cap);

        if (deployment.statuses.contains(&device_id)) {
            let current = deployment.statuses.get_mut(&device_id);
            *current = status;
        } else {
            deployment.statuses.insert(device_id, status);
        };

        event::emit(DeviceStatusUpdated {
            deployment_id: object::id(deployment),
            device_id,
            status,
        });
    }

    public entry fun delete_deployment(deployment: DeploymentRecord, ctx: &TxContext) {
        assert!(ctx.sender() == deployment.creator, ENotCreator);
        let DeploymentRecord {
            id, creator, firmware_name: _, version: _, walrus_blob_id: _,
            sha256_hash: _, target_groups: _, target_devices: _, statuses: _, created_at: _,
        } = deployment;
        event::emit(DeploymentDeleted { deployment_id: id.to_inner(), creator });
        id.delete();
    }

    // ── Read helpers ──────────────────────────────────────────────────────────

    public fun target_groups(d: &DeploymentRecord): &vector<ID> { &d.target_groups }
    public fun target_devices(d: &DeploymentRecord): &vector<ID> { &d.target_devices }
    public fun walrus_blob_id(d: &DeploymentRecord): &String { &d.walrus_blob_id }
    public fun sha256_hash(d: &DeploymentRecord): &String { &d.sha256_hash }
    public fun status_applied(): u8 { STATUS_APPLIED }
    public fun status_failed(): u8 { STATUS_FAILED }
}
