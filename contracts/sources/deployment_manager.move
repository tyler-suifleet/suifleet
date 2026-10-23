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
    const EInvalidControlState: u64 = 9;
    const EInvalidSchedule: u64 = 10;
    const ECancelled: u64 = 11;

    // ── Dynamic field keys ────────────────────────────────────────────────────
    //
    // Typed key structs ensure compile-time safety and prevent key collisions.
    // New fields can be added in future upgrades without touching DeploymentRecord.

    public struct TargetGroupsKey has copy, drop, store {}
    public struct TargetDevicesKey has copy, drop, store {}
    public struct StatusesKey has copy, drop, store {}
    public struct RecipeDigestKey has copy, drop, store {}
    public struct ReceiptsKey has copy, drop, store {}
    /// Present (true) on records that a group's desired state has referenced:
    /// desired-state reconciliation applies them, not the one-shot event path.
    public struct ManagedKey has copy, drop, store {}
    /// The delegate who created a record under a deployer role (`roles`);
    /// the record's `creator` is the operator the role acts for.
    public struct DelegateKey has copy, drop, store {}

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

    /// SF-018 controls have their own introduction-package origin. An absent
    /// field preserves existing immediate-deployment behaviour.
    public struct ControlKey has copy, drop, store {}
    public struct Control has copy, drop, store {
        revision: u64,
        state: u8, // 0 active, 1 paused, 2 permanently cancelled
        not_before_ms: u64,
        expires_ms: u64, // 0 means no expiry
        window_start_ms: u64,
        window_end_ms: u64, // 0 means no maintenance window
        notice_ms: u64,
    }

    /// Creator-only, additive control API for existing and new records.
    /// Cancellation is irreversible. Expiry and windows gate admission, not
    /// an updater already admitted; cancelling never kills an in-flight apply.
    public fun set_control(
        deployment: &mut DeploymentRecord, state: u8,
        not_before_ms: u64, expires_ms: u64,
        window_start_ms: u64, window_end_ms: u64, notice_ms: u64,
        ctx: &TxContext,
    ) {
        assert!(ctx.sender() == deployment.creator, ENotCreator);
        assert!(state <= 2, EInvalidControlState);
        assert!(expires_ms == 0 || expires_ms > not_before_ms, EInvalidSchedule);
        assert!((window_start_ms == 0 && window_end_ms == 0) ||
            window_end_ms > window_start_ms, EInvalidSchedule);
        assert!(notice_ms <= 604800000, EInvalidSchedule); // at most seven days
        let revision = if (df::exists(&deployment.id, ControlKey {})) {
            let previous: Control = df::remove(&mut deployment.id, ControlKey {});
            assert!(previous.state != 2, ECancelled);
            previous.revision + 1
        } else { 1 };
        df::add(&mut deployment.id, ControlKey {}, Control {
            revision, state, not_before_ms, expires_ms,
            window_start_ms, window_end_ms, notice_ms,
        });
    }

    public fun control(deployment: &DeploymentRecord): Control {
        if (df::exists(&deployment.id, ControlKey {})) {
            *df::borrow(&deployment.id, ControlKey {})
        } else {
            Control { revision: 0, state: 0, not_before_ms: 0, expires_ms: 0,
                window_start_ms: 0, window_end_ms: 0, notice_ms: 0 }
        }
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
    #[allow(unused_field)]
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
        let creator = ctx.sender();
        let record = make_deployment(creator, component_name, version, recipe_blob_id, artifact_sha256,
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
        share_deployment(new_deployment_verified(component_name, version, recipe_blob_id, artifact_sha256,
            recipe_sha256, target_groups, target_devices, clock, ctx));
    }

    /// As `create_deployment_verified`, but returns the record so the same
    /// transaction can reference it (e.g. in a desired-state revision) before
    /// `share_deployment` publishes it.
    public fun new_deployment_verified(
        component_name: String,
        version: String,
        recipe_blob_id: String,
        artifact_sha256: String,
        recipe_sha256: String,
        target_groups: vector<ID>,
        target_devices: vector<ID>,
        clock: &Clock,
        ctx: &mut TxContext,
    ): DeploymentRecord {
        assert_recipe_hash(&recipe_sha256);
        let creator = ctx.sender();
        let mut record = make_deployment(creator, component_name, version, recipe_blob_id, artifact_sha256,
            target_groups, target_devices, clock, ctx);
        df::add(&mut record.id, RecipeDigestKey {}, recipe_sha256);
        record
    }

    public fun share_deployment(record: DeploymentRecord) {
        transfer::share_object(record);
    }

    /// A verified group deployment attributed to `operator` but created by the
    /// sender as its delegate. Only `roles::create_deployment_as`, which checks
    /// an active deployer role scoped to these groups, may call it.
    public(package) fun create_delegated(
        operator: address,
        component_name: String,
        version: String,
        recipe_blob_id: String,
        artifact_sha256: String,
        recipe_sha256: String,
        target_groups: vector<ID>,
        clock: &Clock,
        ctx: &mut TxContext,
    ): ID {
        assert_recipe_hash(&recipe_sha256);
        let mut record = make_deployment(operator, component_name, version, recipe_blob_id, artifact_sha256,
            target_groups, vector[], clock, ctx);
        df::add(&mut record.id, RecipeDigestKey {}, recipe_sha256);
        df::add(&mut record.id, DelegateKey {}, ctx.sender());
        let id = object::id(&record);
        transfer::share_object(record);
        id
    }

    fun assert_recipe_hash(recipe_sha256: &String) {
        let bytes = recipe_sha256.as_bytes();
        assert!(bytes.length() == 64, EInvalidRecipeHash);
        let mut i = 0;
        while (i < bytes.length()) {
            let byte = bytes[i];
            assert!((byte >= 48 && byte <= 57) || (byte >= 97 && byte <= 102), EInvalidRecipeHash);
            i = i + 1;
        };
    }

    public(package) fun create_catalog_deployment<K: copy + drop + store, V: store>(
        name: String, version: String, groups: vector<ID>, devices: vector<ID>,
        key: K, value: V, clock: &Clock, ctx: &mut TxContext,
    ) {
        transfer::share_object(new_catalog_deployment(name, version, groups, devices, key, value, clock, ctx));
    }

    public(package) fun new_catalog_deployment<K: copy + drop + store, V: store>(
        name: String, version: String, groups: vector<ID>, devices: vector<ID>,
        key: K, value: V, clock: &Clock, ctx: &mut TxContext,
    ): DeploymentRecord {
        let creator = ctx.sender();
        let mut record = make_deployment(creator, name, version, std::string::utf8(b""),
            std::string::utf8(b""), groups, devices, clock, ctx);
        record.schema_version = 2;
        df::add(&mut record.id, key, value);
        record
    }

    public(package) fun remove_catalog_binding<K: copy + drop + store, V: store + drop>(
        deployment: &mut DeploymentRecord, key: K, ctx: &TxContext,
    ) {
        assert!(ctx.sender() == deployment.creator, ENotCreator);
        let _: V = df::remove(&mut deployment.id, key);
        deployment.schema_version = SCHEMA_VERSION;
    }

    fun make_deployment(
        creator: address,
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
            creator,
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
    #[allow(lint(prefer_mut_tx_context))]
    public fun report_status(
        _deployment: &mut DeploymentRecord,
        _cap: &DeviceCap,
        _status: u8,
        _ctx: &TxContext,
    ) {
        abort EUseVerifiedReport
    }

    /// Superseded: use `report_verified_status_for_group`.
    #[allow(lint(prefer_mut_tx_context))]
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
    #[allow(lint(prefer_mut_tx_context))]
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
    #[allow(lint(prefer_mut_tx_context))]
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
        if (deployment.schema_version == 3) {
            record_rollout_outcome(deployment, device_id, status);
        };
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

    #[allow(lint(prefer_mut_tx_context, public_entry))]
    public entry fun delete_deployment(mut deployment: DeploymentRecord, ctx: &TxContext) {
        assert!(ctx.sender() == deployment.creator, ENotCreator);

        // Catalog records require their defining module to remove the binding first.
        assert!(deployment.schema_version == SCHEMA_VERSION, EPackageOutdated);
        if (df::exists(&deployment.id, ControlKey {})) {
            let _: Control = df::remove(&mut deployment.id, ControlKey {});
        };
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
        if (df::exists(&deployment.id, ManagedKey {})) {
            let _: bool = df::remove(&mut deployment.id, ManagedKey {});
        };
        if (df::exists(&deployment.id, DelegateKey {})) {
            let _: address = df::remove(&mut deployment.id, DelegateKey {});
        };

        let DeploymentRecord { id, creator, component_name: _, version: _, recipe_blob_id: _, artifact_sha256: _, created_at: _, schema_version: _ } = deployment;
        event::emit(DeploymentDeleted { deployment_id: id.to_inner(), creator });
        id.delete();
    }

    /// Authority changes in place so every still-callable package checks the
    /// recipient as creator. Preserve payload, targets, receipts and controls.
    public(package) fun handover(d: &mut DeploymentRecord, recipient: address, ctx: &TxContext) {
        assert!(ctx.sender() == d.creator, ENotCreator);
        // Catalog-bound records resolve dependencies through the original
        // publisher index. Supersede them under a recipient-owned catalog;
        // changing only creator would retain the old publisher as an authority.
        assert!(d.schema_version != 2, EPackageOutdated);
        d.creator = recipient;
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
    public fun creator(d: &DeploymentRecord): address { d.creator }
    public fun component_name(d: &DeploymentRecord): &String { &d.component_name }
    public fun has_recipe_digest(d: &DeploymentRecord): bool { df::exists(&d.id, RecipeDigestKey {}) }
    public fun is_managed(d: &DeploymentRecord): bool { df::exists(&d.id, ManagedKey {}) }
    /// 1 plain, 2 catalog-bound, 3 staged rollout; selects the deletion path.
    public fun schema_version(d: &DeploymentRecord): u64 { d.schema_version }

    /// Marks a record as owned by desired-state reconciliation (idempotent).
    public(package) fun mark_managed(d: &mut DeploymentRecord) {
        if (!df::exists(&d.id, ManagedKey {})) {
            df::add(&mut d.id, ManagedKey {}, true);
        };
    }

    /// The delegate behind a role-created record, if any.
    public fun delegate(d: &DeploymentRecord): Option<address> {
        if (df::exists(&d.id, DelegateKey {})) option::some(*df::borrow(&d.id, DelegateKey {})) else option::none()
    }
    public fun artifact_sha256(d: &DeploymentRecord): &String { &d.artifact_sha256 }
    public fun status_applied(): u8 { STATUS_APPLIED }
    public fun status_failed(): u8 { STATUS_FAILED }
    // SF-017: creator-coordinated, durable staged rollout. Schema 3 makes
    // older clients fail closed rather than ignore this admission policy.
    public struct RolloutKey has copy, drop, store {}
    public struct RolloutPolicy has copy, drop, store {
        concurrency: u64, rate_count: u64, rate_window_ms: u64,
        bake_ms: u64, minimum_samples: u64, failure_bps: u64,
        offline_timeout_ms: u64,
    }
    public struct Rollout has copy, drop, store {
        policy: RolloutPolicy,
        members: vector<ID>, cohorts: vector<u64>,
        admitted_at: vector<u64>, outcomes: vector<u8>, skipped: vector<bool>,
        cohort: u64, state: u8, // 0 active, 1 paused, 2 aborted, 3 complete
        bake_started_ms: u64, progress_ms: u64, revision: u64,
    }
    public struct RolloutDecision has copy, drop {
        deployment_id: ID, revision: u64, cohort: u64, state: u8,
        reason: String, at_ms: u64, admitted: vector<ID>,
    }
    const ERolloutPolicy: u64 = 12;
    const ERolloutState: u64 = 13;

    public fun rollout_policy(concurrency: u64, rate_count: u64, rate_window_ms: u64,
        bake_ms: u64, minimum_samples: u64, failure_bps: u64, offline_timeout_ms: u64,
    ): RolloutPolicy {
        assert!(concurrency > 0 && concurrency <= 512 && rate_count > 0 && rate_count <= 512,
            ERolloutPolicy);
        assert!(rate_window_ms > 0 && rate_window_ms <= 604800000 && bake_ms <= 604800000 &&
            minimum_samples > 0 && minimum_samples <= 512 && failure_bps > 0 && failure_bps <= 10000 &&
            offline_timeout_ms > 0 && offline_timeout_ms <= 604800000, ERolloutPolicy);
        RolloutPolicy { concurrency, rate_count, rate_window_ms, bake_ms, minimum_samples,
            failure_bps, offline_timeout_ms }
    }

    /// Snapshot cohorts are ordered 0..N with at least minimum_samples in
    /// each cohort. Group membership/ownership is still checked by devices.
    /// A cohort snapshot never silently expands when group membership changes.
    public fun new_rollout_verified(component_name: String, version: String,
        recipe_blob_id: String, artifact_sha256: String, recipe_sha256: String,
        target_groups: vector<ID>, members: vector<ID>, cohorts: vector<u64>,
        policy: RolloutPolicy, clock: &Clock, ctx: &mut TxContext,
    ): DeploymentRecord {
        let n = members.length();
        assert!(n > 0 && n <= 512 && cohorts.length() == n && cohorts[0] == 0, ERolloutPolicy);
        let mut i = 0; let mut count = 0; let mut current = 0;
        while (i < n) {
            let mut j = 0;
            while (j < i) { assert!(members[j] != members[i], ERolloutPolicy); j = j + 1; };
            if (cohorts[i] != current) {
                assert!(cohorts[i] == current + 1 && count >= policy.minimum_samples, ERolloutPolicy);
                current = cohorts[i]; count = 0;
            };
            count = count + 1; i = i + 1;
        };
        assert!(count >= policy.minimum_samples, ERolloutPolicy);
        // Direct targets bind the snapshot even if there are no groups. The
        // operator check in the daemon remains mandatory for every member.
        let mut d = new_deployment_verified(component_name, version, recipe_blob_id, artifact_sha256,
            recipe_sha256, target_groups, members, clock, ctx);
        d.schema_version = 3;
        let mut admitted_at = vector[]; let mut outcomes = vector[]; let mut skipped = vector[];
        i = 0;
        while (i < n) { admitted_at.push_back(0); outcomes.push_back(0); skipped.push_back(false); i = i + 1; };
        let r = Rollout { policy, members, cohorts, admitted_at, outcomes, skipped, cohort: 0,
            state: 0, bake_started_ms: 0, progress_ms: clock.timestamp_ms(), revision: 0 };
        df::add(&mut d.id, RolloutKey {}, r);
        audit_rollout(&mut d, std::string::utf8(b"created; no members admitted"), vector[], clock);
        d
    }

    public fun rollout(d: &DeploymentRecord): Rollout {
        assert!(d.schema_version == 3, ERolloutState);
        *df::borrow(&d.id, RolloutKey {})
    }
    public fun rollout_progress(d: &DeploymentRecord): (u8, u64) {
        let r = rollout(d); (r.state, r.cohort)
    }
    #[test_only]
    public fun rollout_receipt_for_testing(d: &mut DeploymentRecord, device: ID, status: u8) {
        record_rollout_outcome(d, device, status);
    }

    // Only this introduction package knows this field. Older callable report
    // paths can still write ordinary receipts, but cannot settle rollout grants.
    // Paused/aborted inflight work may truthfully finish; failure stays sticky.
    fun record_rollout_outcome(d: &mut DeploymentRecord, device: ID, status: u8) {
        assert!(status == STATUS_APPLIED || status == STATUS_FAILED, EInvalidStatus);
        let r: &mut Rollout = df::borrow_mut(&mut d.id, RolloutKey {});
        let (found, i) = r.members.index_of(&device);
        assert!(found && r.admitted_at[i] != 0 && !r.skipped[i], ENotTargeted);
        if (r.outcomes[i] != STATUS_FAILED) { *(&mut r.outcomes[i]) = status; };
    }

    public fun rollout_eligible(d: &DeploymentRecord, device: ID): bool {
        let r = rollout(d);
        let (found, i) = r.members.index_of(&device);
        found && r.admitted_at[i] != 0 && r.outcomes[i] == 0 && !r.skipped[i] && r.state == 0
    }

    /// Tick by the creator, after checking current identity-bound telemetry.
    /// `healthy` must name fresh, verified reports of this deployment installed;
    /// `unhealthy` names verified degraded applied members. This is an explicit
    /// operator attestation, not a claim that Move verified off-chain signatures.
    /// Missing/offline/unverified telemetry never passes a health gate.
    public fun advance_rollout(d: &mut DeploymentRecord, online: vector<ID>,
        healthy: vector<ID>, unhealthy: vector<ID>, clock: &Clock, ctx: &TxContext,
    ) {
        assert!(ctx.sender() == d.creator, ENotCreator);
        assert!(d.schema_version == 3, ERolloutState);
        let control = control(d);
        let r: &mut Rollout = df::borrow_mut(&mut d.id, RolloutKey {});
        assert!(r.state == 0, ERolloutState);
        let now = clock.timestamp_ms();
        assert!(now > 0 && now >= r.progress_ms, ERolloutState);
        let mut samples = 0; let mut failures = 0; let mut inflight = 0;
        let mut recent = 0; let mut all_done = true; let mut all_healthy = true;
        let mut cohort_samples = 0; let mut i = 0;
        while (i < r.members.length()) {
            let id = r.members[i];
            if (r.admitted_at[i] != 0) {
                // A verified health regression is sticky, including when a
                // device later overwrites its ordinary deployment receipt.
                if (r.outcomes[i] == STATUS_APPLIED && unhealthy.contains(&id)) {
                    *(&mut r.outcomes[i]) = STATUS_FAILED; r.progress_ms = now;
                };
                if (r.outcomes[i] != 0) {
                    samples = samples + 1;
                    if (r.outcomes[i] == STATUS_FAILED) { failures = failures + 1; };
                } else { inflight = inflight + 1; };
                if (now - r.admitted_at[i] < r.policy.rate_window_ms) { recent = recent + 1; };
            };
            if (r.cohorts[i] == r.cohort && !r.skipped[i]) {
                if (r.outcomes[i] == 0) { all_done = false; } else { cohort_samples = cohort_samples + 1; };
                if (r.outcomes[i] == STATUS_APPLIED && !healthy.contains(&id)) { all_healthy = false; };
            };
            i = i + 1;
        };
        let mut reason = std::string::utf8(b"waiting for receipts, health or rate capacity");
        let mut admitted = vector[];
        if (samples >= r.policy.minimum_samples && failures * 10000 >= samples * r.policy.failure_bps) {
            r.state = 2; reason = std::string::utf8(b"failure threshold reached; permanently aborted");
        } else if (control.state != 0 || (control.expires_ms != 0 && now >= control.expires_ms)) {
            reason = std::string::utf8(b"deployment paused, cancelled or expired; no new grants");
        } else if (all_done && cohort_samples >= r.policy.minimum_samples && all_healthy) {
            if (r.bake_started_ms == 0) { r.bake_started_ms = now; r.progress_ms = now; };
            if (now - r.bake_started_ms >= r.policy.bake_ms) {
                if (r.cohort == r.cohorts[r.cohorts.length() - 1]) { r.state = 3; reason = std::string::utf8(b"rollout complete"); }
                else { r.cohort = r.cohort + 1; r.bake_started_ms = 0; r.progress_ms = now;
                    reason = std::string::utf8(b"bake and health gate passed; next cohort"); };
            } else { reason = std::string::utf8(b"baking completed cohort"); };
        } else {
            // Fresh health must be present at every tick during the bake.
            r.bake_started_ms = 0;
            i = 0;
            while (i < r.members.length() && inflight < r.policy.concurrency && recent < r.policy.rate_count) {
                let id = r.members[i];
                if (r.cohorts[i] == r.cohort && !r.skipped[i] && r.admitted_at[i] == 0 && online.contains(&id)) {
                    *(&mut r.admitted_at[i]) = now; inflight = inflight + 1; recent = recent + 1;
                    admitted.push_back(id); r.progress_ms = now;
                };
                i = i + 1;
            };
            if (!admitted.is_empty()) { reason = std::string::utf8(b"admitted online cohort members"); }
            else if (now - r.progress_ms >= r.policy.offline_timeout_ms) {
                r.state = 1; reason = std::string::utf8(b"offline or ambiguous cohort stalled; capacity retained");
            };
        };
        audit_rollout(d, reason, admitted, clock);
    }

    /// Audited operator recovery. Aborts cannot resume; skips apply only to
    /// never-admitted members. Never release a possibly executing offline slot.
    public fun control_rollout(d: &mut DeploymentRecord, state: u8, skip: vector<ID>,
        reason: String, clock: &Clock, ctx: &TxContext,
    ) {
        assert!(ctx.sender() == d.creator, ENotCreator);
        assert!(reason.length() > 0 && reason.length() <= 256 && state <= 2, ERolloutPolicy);
        let r: &mut Rollout = df::borrow_mut(&mut d.id, RolloutKey {});
        assert!(r.state < 2, ERolloutState);
        let mut i = 0;
        while (i < skip.length()) {
            let (found, index) = r.members.index_of(&skip[i]);
            assert!(found && r.admitted_at[index] == 0, ERolloutState);
            *(&mut r.skipped[index]) = true; i = i + 1;
        };
        r.state = state; r.bake_started_ms = 0; r.progress_ms = clock.timestamp_ms();
        audit_rollout(d, reason, vector[], clock);
    }

    fun audit_rollout(d: &mut DeploymentRecord, reason: String, admitted: vector<ID>, clock: &Clock) {
        let id = object::id(d);
        let r: &mut Rollout = df::borrow_mut(&mut d.id, RolloutKey {});
        r.revision = r.revision + 1;
        event::emit(RolloutDecision { deployment_id: id, revision: r.revision,
            cohort: r.cohort, state: r.state, reason, at_ms: clock.timestamp_ms(), admitted });
    }

    public fun delete_rollout(mut d: DeploymentRecord, ctx: &TxContext) {
        assert!(ctx.sender() == d.creator, ENotCreator);
        assert!(d.schema_version == 3, ERolloutState);
        let _: Rollout = df::remove(&mut d.id, RolloutKey {});
        d.schema_version = SCHEMA_VERSION;
        delete_deployment(d, ctx);
    }

}
