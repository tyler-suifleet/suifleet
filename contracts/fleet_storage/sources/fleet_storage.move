/// Owner-prepaid Walrus storage for a fleet (SF-083).
///
/// The fleet owner deposits Walrus `Storage` resources and a WAL balance for
/// write fees into a shared `FleetStorage` bound to their `FleetBudget`. A
/// device that may draw from that budget (`fleet_budget::assert_may_draw`:
/// unpaused, its current open and unsuspended `GasAccount`, active registry
/// identity, signer-bound DeviceCap) can register a blob paid from it, and so
/// can the owner, for the artifacts and recipes they upload from the dApp
/// (`register_fleet_blob`):
///
/// - the contract computes the blob's exact encoded size with Walrus's own
///   `encoding::encoded_blob_length` and splits only that much storage from
///   the pool; write fees are capped by the device's remaining period quota;
/// - the blob is registered non-deletable and wrapped under this object, so
///   it is fleet-owned: no device call can delete, shorten or extract it;
/// - only the owner can extend, release (and then burn, receiving the Sui
///   storage rebate) or withdraw unused storage and WAL.
///
/// This lives in its own package so the core `sui_edge` package keeps no
/// Walrus dependency. It is pinned to the Walrus Testnet contracts at tag
/// testnet-v1.59.0 (Move.toml). Every mutating call checks `version`, as in
/// `fleet_budget`, so a later upgrade can retire older package versions.
///
/// Read interface version 1 (SF-092): the getters at the end and the events.
module fleet_storage::fleet_storage {
    use sui::balance::{Self, Balance};
    use sui::clock::Clock;
    use sui::coin::{Self, Coin};
    use sui::dynamic_field as df;
    use sui::dynamic_object_field as dof;
    use sui::event;
    use sui_edge::device_cap::{Self, DeviceCap};
    use sui_edge::device_registry::DeviceRegistry;
    use sui_edge::fleet_budget::{Self, FleetBudget, BudgetOwnerCap, GasAccount};
    use wal::wal::WAL;
    use walrus::blob::Blob;
    use walrus::encoding;
    use walrus::storage_resource::{Self, Storage};
    use walrus::system::System;

    // ── Constants ─────────────────────────────────────────────────────────────

    const VERSION: u64 = 3;
    const INTERFACE_VERSION: u64 = 2;

    // ── Errors ────────────────────────────────────────────────────────────────

    const EWrongVersion: u64 = 0;
    const EWrongBudget: u64 = 1;
    const ENoStorage: u64 = 2;
    const EInsufficientStorage: u64 = 3;
    const EOverQuota: u64 = 4;
    const ETooLarge: u64 = 5;
    const ERateLimited: u64 = 6;
    const EInsufficientWal: u64 = 7;
    const EUnknownBlob: u64 = 8;
    const EInvalidQuota: u64 = 9;
    const ENotUpgrade: u64 = 10;
    const EZeroAmount: u64 = 11;
    const ESlippage: u64 = 12;
    const EInvalidEpochs: u64 = 13;

    // ── State ─────────────────────────────────────────────────────────────────

    /// Per-device upload limits. Bytes are Walrus *encoded* bytes, which is
    /// what storage is reserved and paid in; fees are WAL FROST.
    public struct Quota has copy, drop, store {
        period_ms: u64,
        encoded_bytes_per_period: u64,
        uploads_per_period: u64,
        max_blob_size: u64,
        write_fees_per_period: u64,
    }

    public struct FleetStorage has key {
        id: UID,
        version: u64,
        budget_id: ID,
        /// Prepaid storage: all deposits are fused into one resource, so they
        /// must share an epoch range or be adjacent with the same size.
        /// `refill_pool` realigns it to the current epoch to top it up.
        pool: Option<Storage>,
        wal: Balance<WAL>,
        default_quota: Quota,
        blob_count: u64,
        total_encoded_bytes: u64,
        total_write_fees: u64,
    }

    public struct UsageKey has copy, drop, store { device_id: ID }

    public struct Usage has copy, drop, store {
        quota: Quota,
        period_start_ms: u64,
        encoded_bytes_in_period: u64,
        uploads_in_period: u64,
        write_fees_in_period: u64,
        total_encoded_bytes: u64,
        total_uploads: u64,
    }

    /// Dynamic object field holding a fleet-owned Blob.
    public struct BlobKey has copy, drop, store { blob_object_id: ID }

    /// Dynamic field: who uploaded a blob and when.
    public struct RecordKey has copy, drop, store { blob_object_id: ID }

    public struct BlobRecord has copy, drop, store {
        /// The uploading device; none for the owner's own uploads.
        device_id: Option<ID>,
        blob_id: u256,
        size: u64,
        encoded_size: u64,
        end_epoch: u32,
        uploaded_at_ms: u64,
    }

    // ── Events ────────────────────────────────────────────────────────────────

    public struct StorageCreated has copy, drop { storage_id: ID, budget_id: ID }

    public struct StorageDeposited has copy, drop {
        storage_id: ID,
        size: u64,
        pool_size: u64,
        start_epoch: u32,
        end_epoch: u32,
    }

    public struct StorageWithdrawn has copy, drop { storage_id: ID, size: u64, pool_size: u64 }

    /// Storage bought from Walrus with the fleet's WAL (`buy_storage`,
    /// `extend_pool`); `StorageDeposited` follows with the new pool.
    public struct StoragePurchased has copy, drop {
        storage_id: ID,
        size: u64,
        start_epoch: u32,
        end_epoch: u32,
        cost: u64,
        wal_balance: u64,
    }

    /// The pool after `refill_pool`: it covers [start_epoch, end_epoch).
    /// `dropped_size` is the unused size of a pool that had already expired.
    public struct PoolRefilled has copy, drop {
        storage_id: ID,
        added_size: u64,
        dropped_size: u64,
        pool_size: u64,
        start_epoch: u32,
        end_epoch: u32,
        cost: u64,
        wal_balance: u64,
    }

    public struct WalDeposited has copy, drop { storage_id: ID, amount: u64, balance: u64 }

    public struct WalWithdrawn has copy, drop { storage_id: ID, amount: u64, balance: u64 }

    /// Every upload a device registers.
    public struct UploadRegistered has copy, drop {
        storage_id: ID,
        device_id: ID,
        blob_object_id: ID,
        blob_id: u256,
        size: u64,
        encoded_size: u64,
        write_fee: u64,
        end_epoch: u32,
        encoded_bytes_in_period: u64,
        uploads_in_period: u64,
        pool_size: u64,
        wal_balance: u64,
        at_limit: bool,
    }

    /// Every upload the owner registers (artifacts and recipes from the dApp).
    public struct FleetUploadRegistered has copy, drop {
        storage_id: ID,
        blob_object_id: ID,
        blob_id: u256,
        size: u64,
        encoded_size: u64,
        write_fee: u64,
        end_epoch: u32,
        pool_size: u64,
        wal_balance: u64,
    }

    public struct BlobReleased has copy, drop { storage_id: ID, blob_object_id: ID }

    public struct BlobExtended has copy, drop { storage_id: ID, blob_object_id: ID, end_epoch: u32, fee: u64 }

    public struct QuotaChanged has copy, drop { storage_id: ID, device_id: Option<ID>, quota: Quota }

    // SF-092: reserves are real isolated balances, never fs.wal accounting labels.
    public struct RetentionKey has copy, drop, store {}
    public struct RenewalKey has copy, drop, store { blob_object_id: ID }
    public struct Retention has store {
        executor: address,
        margin: u32,
        wal: Balance<WAL>,
        gas: Balance<sui::sui::SUI>,
        wal_per_epoch: u64,
        gas_per_epoch: u64,
        reward: u64,
        epoch: u32,
        wal_spent: u64,
        gas_spent: u64,
        revision: u64,
        pending_wal: u64,
        pending_count: u64,
        covered_count: u64,
    }
    /// Owner approval for new device archives, finite across the whole fleet.
    /// This later type has its own introduction-package origin.
    public struct DeviceRetentionKey has copy, drop, store {}
    public struct DeviceRetention has copy, drop, store { target: u32, max_fee: u64 }
    public struct DeviceRetentionConfigured has copy, drop {
        storage_id: ID, target: u32, max_fee: u64, enabled: bool,
    }

    public fun configure_device_retention(
        fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap,
        target: u32, max_fee: u64,
    ) {
        assert_owner(fs, budget, cap);
        assert!(df::exists(&fs.id, RetentionKey {}), EInsufficientWal);
        assert!(target > 0 && max_fee > 0, EInvalidEpochs);
        if (df::exists(&fs.id, DeviceRetentionKey {})) {
            let _: DeviceRetention = df::remove(&mut fs.id, DeviceRetentionKey {});
        };
        df::add(&mut fs.id, DeviceRetentionKey {}, DeviceRetention { target, max_fee });
        let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
        r.revision = r.revision + 1;
        event::emit(DeviceRetentionConfigured { storage_id: object::id(fs), target, max_fee, enabled: true });
    }

    public fun clear_device_retention(fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap) {
        assert_owner(fs, budget, cap);
        if (df::exists(&fs.id, DeviceRetentionKey {})) {
            let _: DeviceRetention = df::remove(&mut fs.id, DeviceRetentionKey {});
            let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
            r.revision = r.revision + 1;
            event::emit(DeviceRetentionConfigured { storage_id: object::id(fs), target: 0, max_fee: 0, enabled: false });
        };
    }

    /// Archive whose initial purchase covers its complete owner-approved target.
    public struct FullyPaidArchiveKey has copy, drop, store { blob_object_id: ID }

    /// Legacy v2 vector only; removed during migration. RenewalKey is the index.
    public struct ProtectedBlobsKey has copy, drop, store {}
    public struct PrepaidKey has copy, drop, store { blob_object_id: ID }
    public struct PrepaidCountKey has copy, drop, store {}
    public struct RenewalPrepaid has copy, drop { storage_id: ID, blob_object_id: ID, fee: u64, target: u32 }
    public struct Renewal has copy, drop, store {
        target: u32,
        max_fee: u64,
        last_success_epoch: Option<u32>,
    }
    public struct RetentionConfigured has copy, drop {
        storage_id: ID, executor: address, revision: u64,
    }
    public struct RetentionRisk has copy, drop {
        storage_id: ID, wal_removed: u64, gas_removed: u64,
    }
    public struct RenewalCompleted has copy, drop {
        storage_id: ID, blob_object_id: ID, end_epoch: u32,
        fee: u64, reward: u64, epoch: u32, revision: u64,
    }

    /// Owner explicitly selects bounded executor/policy. All registered blobs
    /// remain in fleet custody; each finite target must be authorized separately.
    public fun configure_retention(
        fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap,
        executor: address, margin: u32, wal_per_epoch: u64,
        gas_per_epoch: u64, reward: u64,
    ) {
        assert_owner(fs, budget, cap);
        assert!(margin > 0 && reward <= gas_per_epoch, EInvalidQuota);
        if (!df::exists(&fs.id, RetentionKey {})) {
            df::add(&mut fs.id, PrepaidCountKey {}, 0u64);
            df::add(&mut fs.id, RetentionKey {}, Retention {
                executor, margin, wal: balance::zero(), gas: balance::zero(),
                wal_per_epoch, gas_per_epoch, reward, epoch: 0,
                wal_spent: 0, gas_spent: 0, revision: 0,
                pending_wal: 0, pending_count: 0, covered_count: 0,
            });
        };
        let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
        r.executor = executor;
        r.margin = margin;
        r.wal_per_epoch = wal_per_epoch;
        r.gas_per_epoch = gas_per_epoch;
        r.reward = reward;
        r.revision = r.revision + 1;
        let revision = r.revision;
        event::emit(RetentionConfigured { storage_id: object::id(fs), executor, revision });
    }

    /// Any wallet may replenish reserves; receiving coins confers no authority.
    public fun fund_retention(fs: &mut FleetStorage, wal: Coin<WAL>, gas: Coin<sui::sui::SUI>) {
        assert!(fs.version == VERSION, EWrongVersion);
        let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
        r.wal.join(wal.into_balance());
        r.gas.join(gas.into_balance());
    }

    /// Owner retains withdrawal rights, with an explicit risk event.
    public fun withdraw_retention(
        fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap,
        wal: u64, gas: u64, ctx: &mut TxContext,
    ): (Coin<WAL>, Coin<sui::sui::SUI>) {
        assert_owner(fs, budget, cap);
        let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
        let wal_coin = coin::take(&mut r.wal, wal, ctx);
        let gas_coin = coin::take(&mut r.gas, gas, ctx);
        event::emit(RetentionRisk { storage_id: object::id(fs), wal_removed: wal, gas_removed: gas });
        (wal_coin, gas_coin)
    }

    public fun authorize_renewal(
        fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap,
        blob_object_id: ID, target: u32, max_fee: u64,
    ) {
        assert_owner(fs, budget, cap);
        authorize_renewal_impl(fs, blob_object_id, target, max_fee)
    }

    fun authorize_renewal_impl(fs: &mut FleetStorage, blob_object_id: ID, target: u32, max_fee: u64) {
        assert!(has_blob(fs, blob_object_id), EUnknownBlob);
        assert!(target > blob(fs, blob_object_id).end_epoch() && max_fee > 0, EInvalidEpochs);
        if (df::exists(&fs.id, FullyPaidArchiveKey { blob_object_id })) {
            let _: bool = df::remove(&mut fs.id, FullyPaidArchiveKey { blob_object_id });
            let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
            r.covered_count = r.covered_count - 1;
        };
        discard_prepaid(fs, blob_object_id);
        let key = RenewalKey { blob_object_id };
        if (df::exists(&fs.id, key)) {
            let previous: Renewal = df::remove(&mut fs.id, key);
            let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
            if (previous.last_success_epoch.is_none()) {
                r.pending_wal = r.pending_wal - previous.max_fee;
                r.pending_count = r.pending_count - 1;
            };
        } else {
            let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
            r.covered_count = r.covered_count + 1;
        };
        let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
        r.pending_wal = r.pending_wal + max_fee;
        r.pending_count = r.pending_count + 1;
        df::add(&mut fs.id, key, Renewal { target, max_fee, last_success_epoch: option::none() });
    }

    /// Buy the actual finite renewal resource before admitting discretionary
    /// work. Walrus computes and charges the current price atomically; a stale
    /// quote cannot masquerade as sufficient reserves. No price oracle is trusted.
    public fun prepay_renewal(
        fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap,
        system: &mut System, blob_object_id: ID, ctx: &mut TxContext,
    ) {
        assert_owner(fs, budget, cap);
        prepay_renewal_impl(fs, system, blob_object_id, ctx)
    }

    fun prepay_renewal_impl(fs: &mut FleetStorage, system: &mut System, blob_object_id: ID, ctx: &mut TxContext) {
        assert!(!dof::exists(&fs.id, PrepaidKey { blob_object_id }), EInvalidEpochs);
        let p: Renewal = *df::borrow(&fs.id, RenewalKey { blob_object_id });
        let b = blob(fs, blob_object_id);
        let (end, size) = (b.end_epoch(), b.storage().size());
        assert!(p.last_success_epoch.is_none() && system.epoch() < end && p.target > end, EInvalidEpochs);
        let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
        if (r.epoch != system.epoch()) {
            r.epoch = system.epoch(); r.wal_spent = 0; r.gas_spent = 0;
        };
        assert!(r.wal_spent <= r.wal_per_epoch && p.max_fee <= r.wal_per_epoch - r.wal_spent, EOverQuota);
        let mut payment = coin::take(&mut r.wal, p.max_fee, ctx);
        let resource = system.reserve_space_for_epochs(size, end, p.target, &mut payment, ctx);
        let fee = p.max_fee - payment.value();
        let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
        r.wal.join(payment.into_balance());
        r.wal_spent = r.wal_spent + fee;
        r.pending_wal = r.pending_wal - p.max_fee;
        let policy: &mut Renewal = df::borrow_mut(&mut fs.id, RenewalKey { blob_object_id });
        policy.max_fee = 0;
        dof::add(&mut fs.id, PrepaidKey { blob_object_id }, resource);
        let count: &mut u64 = df::borrow_mut(&mut fs.id, PrepaidCountKey {});
        *count = *count + 1;
        event::emit(RenewalPrepaid { storage_id: object::id(fs), blob_object_id, fee, target: p.target });
    }

    /// Explicit owner overrides discard already paid capacity, never refund WAL.
    fun discard_prepaid(fs: &mut FleetStorage, blob_object_id: ID) {
        let key = PrepaidKey { blob_object_id };
        if (dof::exists(&fs.id, key)) {
            storage_resource::destroy(dof::remove<PrepaidKey, Storage>(&mut fs.id, key));
            let count: &mut u64 = df::borrow_mut(&mut fs.id, PrepaidCountKey {});
            *count = *count - 1;
            event::emit(RetentionRisk { storage_id: object::id(fs), wal_removed: 0, gas_removed: 0 });
        };
    }

    /// Restricted execution: no owner cap, arbitrary blob, withdrawal or policy
    /// change. The shared object serializes races; duplicates abort before spend.
    /// Walrus independently enforces certification, live expiry and horizon.
    /// Reward is intentionally paid to the authenticated configured executor.
    #[allow(lint(self_transfer))]
    public fun execute_renewal(
        fs: &mut FleetStorage, system: &mut System, blob_object_id: ID,
        expected_end: u32, expected_revision: u64, ctx: &mut TxContext,
    ) {
        assert!(fs.version == VERSION, EWrongVersion);
        let now = system.epoch();
        let policy: Renewal = *df::borrow(&fs.id, RenewalKey { blob_object_id });
        let end = blob(fs, blob_object_id).end_epoch();
        assert!(end == expected_end && end < policy.target && now < end, EInvalidEpochs);
        let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
        assert!(ctx.sender() == r.executor && expected_revision == r.revision, EWrongBudget);
        assert!(end - now <= r.margin, EInvalidEpochs);
        if (now != r.epoch) {
            r.epoch = now;
            r.wal_spent = 0;
            r.gas_spent = 0;
        };
        assert!(r.gas_spent <= r.gas_per_epoch && r.reward <= r.gas_per_epoch - r.gas_spent, EOverQuota);
        assert!(r.gas.value() >= r.reward, EInsufficientWal);
        let resource: Storage = dof::remove(&mut fs.id, PrepaidKey { blob_object_id });
        let blob: &mut Blob = dof::borrow_mut(&mut fs.id, BlobKey { blob_object_id });
        system.extend_blob_with_resource(blob, resource);
        assert!(blob.end_epoch() == policy.target, EInvalidEpochs);
        let count: &mut u64 = df::borrow_mut(&mut fs.id, PrepaidCountKey {});
        *count = *count - 1;
        let fee = 0;
        let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
        r.pending_wal = r.pending_wal - policy.max_fee;
        r.pending_count = r.pending_count - 1;
        r.wal_spent = r.wal_spent + fee;
        r.gas_spent = r.gas_spent + r.reward;
        let reward = r.reward;
        let revision = r.revision;
        transfer::public_transfer(coin::take(&mut r.gas, reward, ctx), ctx.sender());
        let record: &mut BlobRecord = df::borrow_mut(&mut fs.id, RecordKey { blob_object_id });
        record.end_epoch = policy.target;
        let auth: &mut Renewal = df::borrow_mut(&mut fs.id, RenewalKey { blob_object_id });
        auth.last_success_epoch = option::some(now);
        event::emit(RenewalCompleted { storage_id: object::id(fs), blob_object_id,
            end_epoch: policy.target, fee, reward, epoch: now, revision });
    }

    public fun retention_balances(fs: &FleetStorage): (u64, u64) {
        if (!df::exists(&fs.id, RetentionKey {})) return (0, 0);
        let r: &Retention = df::borrow(&fs.id, RetentionKey {});
        (r.wal.value(), r.gas.value())
    }
    public fun renewal(fs: &FleetStorage, blob_object_id: ID): Option<Renewal> {
        if (!df::exists(&fs.id, RenewalKey { blob_object_id })) return option::none();
        option::some(*df::borrow(&fs.id, RenewalKey { blob_object_id }))
    }

    // ── Owner ─────────────────────────────────────────────────────────────────

    public fun new_quota(
        period_ms: u64,
        encoded_bytes_per_period: u64,
        uploads_per_period: u64,
        max_blob_size: u64,
        write_fees_per_period: u64,
    ): Quota {
        assert!(period_ms > 0 && uploads_per_period > 0, EInvalidQuota);
        Quota { period_ms, encoded_bytes_per_period, uploads_per_period, max_blob_size, write_fees_per_period }
    }

    /// Share storage for `budget`; only its owner may manage it.
    public fun create(budget: &FleetBudget, cap: &BudgetOwnerCap, default_quota: Quota, ctx: &mut TxContext) {
        fleet_budget::assert_owner(budget, cap);
        let storage = FleetStorage {
            id: object::new(ctx),
            version: VERSION,
            budget_id: object::id(budget),
            pool: option::none(),
            wal: balance::zero(),
            default_quota,
            blob_count: 0,
            total_encoded_bytes: 0,
            total_write_fees: 0,
        };
        event::emit(StorageCreated { storage_id: object::id(&storage), budget_id: storage.budget_id });
        transfer::share_object(storage);
    }

    public fun deposit_storage(fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap, storage: Storage) {
        assert_owner(fs, budget, cap);
        add_to_pool(fs, storage);
    }

    /// Buy `size` bytes of storage for `epochs_ahead` epochs from Walrus with
    /// the fleet's WAL and add it to the pool, in one call: no WAL leaves the
    /// fleet. It must fuse with the pool (empty, the same epoch range, or the
    /// same size and adjacent); otherwise Walrus aborts and nothing is spent.
    /// Returns the WAL cost.
    public fun buy_storage(
        fs: &mut FleetStorage,
        budget: &FleetBudget,
        cap: &BudgetOwnerCap,
        system: &mut System,
        size: u64,
        epochs_ahead: u32,
        ctx: &mut TxContext,
    ): u64 {
        assert_owner(fs, budget, cap);
        assert!(size > 0 && epochs_ahead > 0, EZeroAmount);
        assert_retention_funded(fs);
        let mut payment = coin::from_balance(fs.wal.withdraw_all(), ctx);
        let before = payment.value();
        let storage = system.reserve_space(size, epochs_ahead, &mut payment, ctx);
        purchased(fs, storage, before - payment.value(), payment)
    }

    /// Extend the whole pool's expiry by `epochs`, buying the same size for
    /// the epochs right after it with the fleet's WAL. Blobs already
    /// registered keep their own end epochs (see `extend_blob`). Returns the
    /// WAL cost.
    public fun extend_pool(
        fs: &mut FleetStorage,
        budget: &FleetBudget,
        cap: &BudgetOwnerCap,
        system: &mut System,
        epochs: u32,
        ctx: &mut TxContext,
    ): u64 {
        assert_owner(fs, budget, cap);
        assert!(fs.pool.is_some(), ENoStorage);
        assert!(epochs > 0, EZeroAmount);
        let (size, end) = (fs.pool.borrow().size(), fs.pool.borrow().end_epoch());
        assert_retention_funded(fs);
        let mut payment = coin::from_balance(fs.wal.withdraw_all(), ctx);
        let before = payment.value();
        let storage = system.reserve_space_for_epochs(size, end, end + epochs, &mut payment, ctx);
        purchased(fs, storage, before - payment.value(), payment)
    }

    /// Top up the pool and keep it until `end_epoch`, buying from Walrus with
    /// the fleet's WAL; this works at any epoch, including after the pool
    /// expired. Walrus joins storage only over the same epoch range (or
    /// adjacent ranges of the same size), so storage bought after the pool's
    /// first epoch cannot join it directly. This call therefore:
    ///
    /// - drops the pool's elapsed epochs, which no new blob can use, and an
    ///   expired pool entirely (its Sui storage rebate goes to the sender);
    /// - extends what is left, at its size, to `end_epoch`;
    /// - buys `add_size` more bytes for the same range.
    ///
    /// The pool then covers [current epoch, `end_epoch`). `end_epoch` cannot
    /// shorten an unexpired pool. Registered blobs keep their own storage and
    /// end epochs (see `extend_blob`). Returns the WAL cost.
    public fun refill_pool(
        fs: &mut FleetStorage,
        budget: &FleetBudget,
        cap: &BudgetOwnerCap,
        system: &mut System,
        add_size: u64,
        end_epoch: u32,
        ctx: &mut TxContext,
    ): u64 {
        assert_owner(fs, budget, cap);
        let now = system.epoch();
        refill(fs, system, now, add_size, end_epoch, ctx)
    }

    /// Take `size` bytes of unused storage back out of the pool.
    public fun withdraw_storage(fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap, size: u64, ctx: &mut TxContext): Storage {
        assert_owner(fs, budget, cap);
        let storage = take_storage(fs, size, ctx);
        event::emit(StorageWithdrawn { storage_id: object::id(fs), size, pool_size: pool_size(fs) });
        storage
    }

    public fun deposit_wal(fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap, coin: Coin<WAL>) {
        assert_owner(fs, budget, cap);
        let amount = coin.value();
        assert!(amount > 0, EZeroAmount);
        fs.wal.join(coin.into_balance());
        event::emit(WalDeposited { storage_id: object::id(fs), amount, balance: fs.wal.value() });
    }

    /// Deposit WAL bought earlier in the same transaction (for example a
    /// SUI→WAL swap), aborting the whole transaction unless at least
    /// `min_amount` arrived: the slippage limit of an in-PTB swap.
    public fun deposit_wal_at_least(fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap, coin: Coin<WAL>, min_amount: u64) {
        assert!(coin.value() >= min_amount, ESlippage);
        deposit_wal(fs, budget, cap, coin);
    }

    public fun withdraw_wal(fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap, amount: u64, ctx: &mut TxContext): Coin<WAL> {
        assert_owner(fs, budget, cap);
        assert!(amount > 0, EZeroAmount);
        assert!(fs.wal.value() >= amount, EInsufficientWal);
        let coin = coin::take(&mut fs.wal, amount, ctx);
        event::emit(WalWithdrawn { storage_id: object::id(fs), amount, balance: fs.wal.value() });
        coin
    }

    public fun set_default_quota(fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap, quota: Quota) {
        assert_owner(fs, budget, cap);
        fs.default_quota = quota;
        event::emit(QuotaChanged { storage_id: object::id(fs), device_id: option::none(), quota });
    }

    public fun set_device_quota(fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap, device_id: ID, quota: Quota, clock: &Clock) {
        assert_owner(fs, budget, cap);
        let now = clock.timestamp_ms();
        usage_mut(fs, device_id, now).quota = quota;
        event::emit(QuotaChanged { storage_id: object::id(fs), device_id: option::some(device_id), quota });
    }

    /// Extend a fleet blob by `epochs`, paying from the WAL balance.
    public fun extend_blob(
        fs: &mut FleetStorage,
        budget: &FleetBudget,
        cap: &BudgetOwnerCap,
        system: &mut System,
        blob_object_id: ID,
        epochs: u32,
        ctx: &mut TxContext,
    ) {
        assert_owner(fs, budget, cap);
        assert!(dof::exists(&fs.id, BlobKey { blob_object_id }), EUnknownBlob);
        assert_retention_funded(fs);
        let mut payment = coin::from_balance(fs.wal.withdraw_all(), ctx);
        let before = payment.value();
        let blob: &mut Blob = dof::borrow_mut(&mut fs.id, BlobKey { blob_object_id });
        system.extend_blob(blob, epochs, &mut payment);
        let end_epoch = blob.end_epoch();
        let fee = before - payment.value();
        fs.wal.join(payment.into_balance());
        let record: &mut BlobRecord = df::borrow_mut(&mut fs.id, RecordKey { blob_object_id });
        record.end_epoch = end_epoch;
        event::emit(BlobExtended { storage_id: object::id(fs), blob_object_id, end_epoch, fee });
    }

    /// Browser-triggered extension for a device archive. Never strands an
    /// attached legacy renewal/prepayment; handle that policy first.
    public fun extend_stream_blob(fs: &mut FleetStorage, budget: &FleetBudget,
        cap: &BudgetOwnerCap, system: &mut System, blob_object_id: ID,
        epochs: u32, ctx: &mut TxContext) {
        assert_owner(fs, budget, cap);
        assert!(epochs > 0 && epochs <= 53, EInvalidEpochs);
        assert!(blob_record(fs, blob_object_id).destroy_some().device_id.is_some(), EUnknownBlob);
        assert!(!df::exists(&fs.id, RenewalKey { blob_object_id }), EInvalidEpochs);
        extend_blob(fs, budget, cap, system, blob_object_id, epochs, ctx);
    }

    /// Hand a fleet blob to the owner, for example to burn it after expiry
    /// and receive its Sui storage rebate.
    public fun release_blob(fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap, blob_object_id: ID): Blob {
        assert_owner(fs, budget, cap);
        assert!(dof::exists(&fs.id, BlobKey { blob_object_id }), EUnknownBlob);
        discard_prepaid(fs, blob_object_id);
        if (df::exists(&fs.id, RenewalKey { blob_object_id })) {
            let previous: Renewal = df::remove(&mut fs.id, RenewalKey { blob_object_id });
            let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
            r.covered_count = r.covered_count - 1;
            if (previous.last_success_epoch.is_none()) {
                r.pending_count = r.pending_count - 1;
                r.pending_wal = r.pending_wal - previous.max_fee;
            };
            event::emit(RetentionRisk { storage_id: object::id(fs), wal_removed: 0, gas_removed: 0 });
        };
        if (df::exists(&fs.id, FullyPaidArchiveKey { blob_object_id })) {
            let _: bool = df::remove(&mut fs.id, FullyPaidArchiveKey { blob_object_id });
            let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
            r.covered_count = r.covered_count - 1;
        };
        let _: BlobRecord = df::remove(&mut fs.id, RecordKey { blob_object_id });
        fs.blob_count = fs.blob_count - 1;
        event::emit(BlobReleased { storage_id: object::id(fs), blob_object_id });
        dof::remove(&mut fs.id, BlobKey { blob_object_id })
    }

    public fun migrate(fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap) {
        fleet_budget::assert_owner(budget, cap);
        assert!(object::id(budget) == fs.budget_id, EWrongBudget);
        assert!(fs.version < VERSION, ENotUpgrade);
        // Layouts and keyed policies/prepayments remain unchanged. Removing this
        // bounded legacy vector neither releases blobs nor changes coverage.
        if (df::exists(&fs.id, ProtectedBlobsKey {})) {
            let _: vector<ID> = df::remove(&mut fs.id, ProtectedBlobsKey {});
        };
        fs.version = VERSION;
    }

    // ── Device ────────────────────────────────────────────────────────────────

    /// Register a blob for the sending device, paid from the fleet's
    /// storage and WAL within the device's quota. Returns the Blob's object
    /// ID; the blob stays wrapped here. Certify it with `certify_blob`.
    public fun register_blob(
        fs: &mut FleetStorage,
        budget: &FleetBudget,
        account: &GasAccount,
        registry: &DeviceRegistry,
        cap: &DeviceCap,
        system: &mut System,
        blob_id: u256,
        root_hash: u256,
        size: u64,
        encoding_type: u8,
        clock: &Clock,
        ctx: &mut TxContext,
    ): ID {
        register_impl(fs, budget, account, registry, cap, system, blob_id, root_hash,
            size, encoding_type, option::none(), clock, ctx)
    }

    /// Buy the complete finite lifetime at write time. Both capacity and write
    /// fees consume the SAME device WAL quota; no owner authority is delegated.
    public fun register_blob_for_epochs(
        fs: &mut FleetStorage, budget: &FleetBudget, account: &GasAccount,
        registry: &DeviceRegistry, cap: &DeviceCap, system: &mut System,
        blob_id: u256, root_hash: u256, size: u64, encoding_type: u8,
        epochs: u32, clock: &Clock, ctx: &mut TxContext,
    ): ID {
        assert!(epochs > 0 && epochs <= 53, EInvalidEpochs);
        register_impl(fs, budget, account, registry, cap, system, blob_id, root_hash,
            size, encoding_type, option::some(epochs), clock, ctx)
    }

    fun register_impl(
        fs: &mut FleetStorage, budget: &FleetBudget, account: &GasAccount,
        registry: &DeviceRegistry, cap: &DeviceCap, system: &mut System,
        blob_id: u256, root_hash: u256, size: u64, encoding_type: u8,
        epochs: Option<u32>, clock: &Clock, ctx: &mut TxContext,
    ): ID {
        let retained = df::exists(&fs.id, RetentionKey {});
        assert!(!retained || df::exists(&fs.id, DeviceRetentionKey {}), EInsufficientWal);
        assert!(fs.version == VERSION, EWrongVersion);
        assert!(object::id(budget) == fs.budget_id, EWrongBudget);
        fleet_budget::assert_may_draw(budget, account, registry, cap, ctx);
        let device_id = device_cap::device_id(cap);
        let encoded_size = encoding::encoded_blob_length(size, encoding_type, system.n_shards());
        let now = clock.timestamp_ms();
        let usage = *usage_mut(fs, device_id, now);
        let quota = usage.quota;
        assert!(size <= quota.max_blob_size, ETooLarge);
        assert!(usage.uploads_in_period < quota.uploads_per_period, ERateLimited);
        assert!(usage.encoded_bytes_in_period + encoded_size <= quota.encoded_bytes_per_period, EOverQuota);
        assert_retention_funded(fs);
        // The write fee comes from the WAL balance, at most the quota's rest.
        // A lowered quota can leave nothing; the registration then aborts in Walrus.
        let fee_room = if (usage.write_fees_in_period >= quota.write_fees_per_period) 0
            else quota.write_fees_per_period - usage.write_fees_in_period;
        let offered = if (fs.wal.value() < fee_room) fs.wal.value() else fee_room;
        let mut payment = coin::take(&mut fs.wal, offered, ctx);
        let storage = if (epochs.is_some()) {
            system.reserve_space(encoded_size, epochs.destroy_some(), &mut payment, ctx)
        } else take_storage(fs, encoded_size, ctx);
        let blob = system.register_blob(storage, blob_id, root_hash, size, encoding_type, false, &mut payment, ctx);
        let fee = offered - payment.value();
        fs.wal.join(payment.into_balance());
        let blob_object_id = object::id(&blob);
        let end_epoch = blob.end_epoch();
        let usage = usage_mut(fs, device_id, now);
        usage.encoded_bytes_in_period = usage.encoded_bytes_in_period + encoded_size;
        usage.uploads_in_period = usage.uploads_in_period + 1;
        usage.write_fees_in_period = usage.write_fees_in_period + fee;
        usage.total_encoded_bytes = usage.total_encoded_bytes + encoded_size;
        usage.total_uploads = usage.total_uploads + 1;
        let bytes_in_period = usage.encoded_bytes_in_period;
        let uploads_in_period = usage.uploads_in_period;
        let at_limit = uploads_in_period >= quota.uploads_per_period || bytes_in_period >= quota.encoded_bytes_per_period;
        fs.blob_count = fs.blob_count + 1;
        fs.total_encoded_bytes = fs.total_encoded_bytes + encoded_size;
        fs.total_write_fees = fs.total_write_fees + fee;
        df::add(&mut fs.id, RecordKey { blob_object_id }, BlobRecord { device_id: option::some(device_id), blob_id, size, encoded_size, end_epoch, uploaded_at_ms: now });
        dof::add(&mut fs.id, BlobKey { blob_object_id }, blob);
        event::emit(UploadRegistered {
            storage_id: object::id(fs),
            device_id,
            blob_object_id,
            blob_id,
            size,
            encoded_size,
            write_fee: fee,
            end_epoch,
            encoded_bytes_in_period: bytes_in_period,
            uploads_in_period,
            pool_size: pool_size(fs),
            wal_balance: fs.wal.value(),
            at_limit,
        });
        if (retained) {
            let policy: DeviceRetention = *df::borrow(&fs.id, DeviceRetentionKey {});
            // A fully paid rolling stream may outlive the old absolute floor.
            // Legacy pool writes still require its still-future authorization.
            assert!(epochs.is_some() || policy.target > system.epoch(), EInvalidEpochs);
            // Stream writes are fully paid at admission; never create work
            // for the legacy executor. An incompatible owner floor aborts.
            assert!(epochs.is_none() || policy.target <= end_epoch, EInvalidEpochs);
            if (policy.target > end_epoch) {
                authorize_renewal_impl(fs, blob_object_id, policy.target, policy.max_fee);
                prepay_renewal_impl(fs, system, blob_object_id, ctx);
            } else {
                // This archive's complete owner-approved lifetime is already
                // purchased. It needs no future renewal WAL, gas or executor.
                df::add(&mut fs.id, FullyPaidArchiveKey { blob_object_id }, true);
                let r: &mut Retention = df::borrow_mut(&mut fs.id, RetentionKey {});
                r.covered_count = r.covered_count + 1;
            };
            assert_retention_funded(fs);
        };
        blob_object_id
    }

    /// Register a blob the owner uploads (for example a release artifact),
    /// paid from the fleet's storage and WAL and kept here, fleet-owned and
    /// non-deletable, like device uploads. Certify it with `certify_blob`.
    public fun register_fleet_blob(
        fs: &mut FleetStorage,
        budget: &FleetBudget,
        cap: &BudgetOwnerCap,
        system: &mut System,
        blob_id: u256,
        root_hash: u256,
        size: u64,
        encoding_type: u8,
        clock: &Clock,
        ctx: &mut TxContext,
    ): ID {
        assert!(!df::exists(&fs.id, RetentionKey {}), EInsufficientWal);
        register_owner_impl(fs, budget, cap, system, blob_id, root_hash, size, encoding_type, clock, ctx)
    }

    /// Register and reserve a finite owner-approved renewal atomically. Legacy
    /// registration cannot create an unprotected blob while retention is active.
    public fun register_retained_fleet_blob(
        fs: &mut FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap,
        system: &mut System, blob_id: u256, root_hash: u256, size: u64,
        encoding_type: u8, clock: &Clock, target: u32, max_fee: u64, ctx: &mut TxContext,
    ): ID {
        assert_retention_funded(fs);
        assert!(df::exists(&fs.id, RetentionKey {}), EInsufficientWal);
        let id = register_owner_impl(fs, budget, cap, system, blob_id, root_hash, size, encoding_type, clock, ctx);
        let record = blob_record(fs, id).destroy_some();
        assert!(target > record.end_epoch, EInvalidEpochs);
        authorize_renewal(fs, budget, cap, id, target, max_fee);
        prepay_renewal(fs, budget, cap, system, id, ctx);
        assert_retention_funded(fs);
        id
    }

    fun register_owner_impl(
        fs: &mut FleetStorage,
        budget: &FleetBudget,
        cap: &BudgetOwnerCap,
        system: &mut System,
        blob_id: u256,
        root_hash: u256,
        size: u64,
        encoding_type: u8,
        clock: &Clock,
        ctx: &mut TxContext,
    ): ID {
        assert_owner(fs, budget, cap);
        let encoded_size = encoding::encoded_blob_length(size, encoding_type, system.n_shards());
        assert_retention_funded(fs);
        let storage = take_storage(fs, encoded_size, ctx);
        let offered = fs.wal.value();
        let mut payment = coin::take(&mut fs.wal, offered, ctx);
        let blob = system.register_blob(storage, blob_id, root_hash, size, encoding_type, false, &mut payment, ctx);
        let fee = offered - payment.value();
        fs.wal.join(payment.into_balance());
        let blob_object_id = object::id(&blob);
        let end_epoch = blob.end_epoch();
        fs.blob_count = fs.blob_count + 1;
        fs.total_encoded_bytes = fs.total_encoded_bytes + encoded_size;
        fs.total_write_fees = fs.total_write_fees + fee;
        df::add(&mut fs.id, RecordKey { blob_object_id }, BlobRecord { device_id: option::none(), blob_id, size, encoded_size, end_epoch, uploaded_at_ms: clock.timestamp_ms() });
        dof::add(&mut fs.id, BlobKey { blob_object_id }, blob);
        event::emit(FleetUploadRegistered {
            storage_id: object::id(fs),
            blob_object_id,
            blob_id,
            size,
            encoded_size,
            write_fee: fee,
            end_epoch,
            pool_size: pool_size(fs),
            wal_balance: fs.wal.value(),
        });
        blob_object_id
    }

    /// Certify a fleet blob with the storage nodes' certificate. Walrus
    /// verifies the certificate; certification cannot shorten or delete.
    public fun certify_blob(
        fs: &mut FleetStorage,
        system: &System,
        blob_object_id: ID,
        signature: vector<u8>,
        signers_bitmap: vector<u8>,
        message: vector<u8>,
    ) {
        assert!(fs.version == VERSION, EWrongVersion);
        assert!(dof::exists(&fs.id, BlobKey { blob_object_id }), EUnknownBlob);
        let blob: &mut Blob = dof::borrow_mut(&mut fs.id, BlobKey { blob_object_id });
        system.certify_blob(blob, signature, signers_bitmap, message);
    }

    /// Fail closed on incomplete fleet closure or inadequate prepaid ceilings.
    /// All fleet blobs form a conservative superset of active release closure.
    fun assert_retention_funded(fs: &FleetStorage) {
        if (!df::exists(&fs.id, RetentionKey {})) return;
        let r: &Retention = df::borrow(&fs.id, RetentionKey {});
        assert!(r.covered_count == fs.blob_count, EInsufficientWal);
        let prepaid: &u64 = df::borrow(&fs.id, PrepaidCountKey {});
        assert!(*prepaid == r.pending_count, EInsufficientWal);
        assert!(r.wal.value() >= r.pending_wal, EInsufficientWal);
        assert!((r.gas.value() as u128) >= (r.pending_count as u128) * (r.reward as u128), EInsufficientWal);
    }

    // ── Internal ──────────────────────────────────────────────────────────────

    fun assert_owner(fs: &FleetStorage, budget: &FleetBudget, cap: &BudgetOwnerCap) {
        assert!(fs.version == VERSION, EWrongVersion);
        assert!(object::id(budget) == fs.budget_id, EWrongBudget);
        fleet_budget::assert_owner(budget, cap);
    }

    fun add_to_pool(fs: &mut FleetStorage, storage: Storage) {
        let size = storage.size();
        assert!(size > 0, EZeroAmount);
        if (fs.pool.is_none()) fs.pool.fill(storage) else fs.pool.borrow_mut().fuse(storage);
        let pool = fs.pool.borrow();
        event::emit(StorageDeposited {
            storage_id: object::id(fs),
            size,
            pool_size: pool.size(),
            start_epoch: pool.start_epoch(),
            end_epoch: pool.end_epoch(),
        });
    }

    /// `refill_pool` at Walrus epoch `now`.
    fun refill(fs: &mut FleetStorage, system: &mut System, now: u32, add_size: u64, end_epoch: u32, ctx: &mut TxContext): u64 {
        assert!(end_epoch > now, EInvalidEpochs);
        assert_retention_funded(fs);
        let mut payment = coin::from_balance(fs.wal.withdraw_all(), ctx);
        let before = payment.value();
        let mut live = if (fs.pool.is_some()) option::some(fs.pool.extract()) else option::none();
        let mut dropped_size = 0;
        if (live.is_some() && live.borrow().end_epoch() <= now) {
            let expired = live.extract();
            dropped_size = expired.size();
            expired.destroy();
        };
        if (live.is_some() && live.borrow().start_epoch() < now) {
            // split_by_epoch keeps the elapsed epochs in place and returns the rest.
            let rest = live.borrow_mut().split_by_epoch(now, ctx);
            live.swap(rest).destroy();
        };
        let mut start = now;
        if (live.is_some()) {
            let (size, live_start, live_end) = (live.borrow().size(), live.borrow().start_epoch(), live.borrow().end_epoch());
            assert!(end_epoch >= live_end, EInvalidEpochs);
            start = live_start;
            if (end_epoch > live_end) {
                let extension = system.reserve_space_for_epochs(size, live_end, end_epoch, &mut payment, ctx);
                live.borrow_mut().fuse_periods(extension);
            };
        };
        if (add_size > 0) {
            let added = system.reserve_space_for_epochs(add_size, start, end_epoch, &mut payment, ctx);
            if (live.is_some()) live.borrow_mut().fuse_amount(added) else live.fill(added);
        };
        assert!(live.is_some(), EZeroAmount);
        fs.pool.fill(live.destroy_some());
        let cost = before - payment.value();
        fs.wal.join(payment.into_balance());
        let pool = fs.pool.borrow();
        event::emit(PoolRefilled {
            storage_id: object::id(fs),
            added_size: add_size,
            dropped_size,
            pool_size: pool.size(),
            start_epoch: pool.start_epoch(),
            end_epoch: pool.end_epoch(),
            cost,
            wal_balance: fs.wal.value(),
        });
        cost
    }

    /// Return the unspent WAL, record the purchase and add the storage.
    fun purchased(fs: &mut FleetStorage, storage: Storage, cost: u64, change: Coin<WAL>): u64 {
        fs.wal.join(change.into_balance());
        event::emit(StoragePurchased {
            storage_id: object::id(fs),
            size: storage.size(),
            start_epoch: storage.start_epoch(),
            end_epoch: storage.end_epoch(),
            cost,
            wal_balance: fs.wal.value(),
        });
        add_to_pool(fs, storage);
        cost
    }

    fun take_storage(fs: &mut FleetStorage, size: u64, ctx: &mut TxContext): Storage {
        assert!(size > 0, EZeroAmount);
        assert!(fs.pool.is_some(), ENoStorage);
        let available = fs.pool.borrow().size();
        assert!(available >= size, EInsufficientStorage);
        if (available == size) return fs.pool.extract();
        // split_by_size keeps `size` in place and returns the rest.
        let rest = fs.pool.borrow_mut().split_by_size(size, ctx);
        let taken = fs.pool.swap(rest);
        taken
    }

    fun usage_mut(fs: &mut FleetStorage, device_id: ID, now: u64): &mut Usage {
        if (!df::exists(&fs.id, UsageKey { device_id })) {
            let quota = fs.default_quota;
            df::add(&mut fs.id, UsageKey { device_id }, Usage {
                quota,
                period_start_ms: now,
                encoded_bytes_in_period: 0,
                uploads_in_period: 0,
                write_fees_in_period: 0,
                total_encoded_bytes: 0,
                total_uploads: 0,
            });
        };
        let usage: &mut Usage = df::borrow_mut(&mut fs.id, UsageKey { device_id });
        if (now >= usage.period_start_ms + usage.quota.period_ms) {
            usage.period_start_ms = now;
            usage.encoded_bytes_in_period = 0;
            usage.uploads_in_period = 0;
            usage.write_fees_in_period = 0;
        };
        usage
    }

    // ── Reads (interface version 1) ───────────────────────────────────────────

    public fun interface_version(): u64 { INTERFACE_VERSION }
    public fun package_version(): u64 { VERSION }
    public fun version(fs: &FleetStorage): u64 { fs.version }
    public fun budget_id(fs: &FleetStorage): ID { fs.budget_id }
    public fun pool_size(fs: &FleetStorage): u64 { if (fs.pool.is_some()) fs.pool.borrow().size() else 0 }
    /// The pool's Walrus epoch range, if any storage is held. Uploads end
    /// with the pool: `end_epoch` is the fleet storage expiry.
    public fun pool_epochs(fs: &FleetStorage): Option<vector<u32>> {
        if (fs.pool.is_none()) return option::none();
        let pool = fs.pool.borrow();
        option::some(vector[pool.start_epoch(), pool.end_epoch()])
    }
    public fun wal_balance(fs: &FleetStorage): u64 { fs.wal.value() }
    public fun default_quota(fs: &FleetStorage): Quota { fs.default_quota }
    public fun blob_count(fs: &FleetStorage): u64 { fs.blob_count }
    public fun total_encoded_bytes(fs: &FleetStorage): u64 { fs.total_encoded_bytes }
    public fun total_write_fees(fs: &FleetStorage): u64 { fs.total_write_fees }
    public fun usage(fs: &FleetStorage, device_id: ID): Option<Usage> {
        if (!df::exists(&fs.id, UsageKey { device_id })) return option::none();
        option::some(*df::borrow(&fs.id, UsageKey { device_id }))
    }
    public fun blob_record(fs: &FleetStorage, blob_object_id: ID): Option<BlobRecord> {
        if (!df::exists(&fs.id, RecordKey { blob_object_id })) return option::none();
        option::some(*df::borrow(&fs.id, RecordKey { blob_object_id }))
    }
    public fun has_blob(fs: &FleetStorage, blob_object_id: ID): bool { dof::exists(&fs.id, BlobKey { blob_object_id }) }
    public fun blob(fs: &FleetStorage, blob_object_id: ID): &Blob { dof::borrow(&fs.id, BlobKey { blob_object_id }) }

    public fun quota_period_ms(q: &Quota): u64 { q.period_ms }
    public fun quota_encoded_bytes_per_period(q: &Quota): u64 { q.encoded_bytes_per_period }
    public fun quota_uploads_per_period(q: &Quota): u64 { q.uploads_per_period }
    public fun quota_max_blob_size(q: &Quota): u64 { q.max_blob_size }
    public fun quota_write_fees_per_period(q: &Quota): u64 { q.write_fees_per_period }

    public fun usage_quota(u: &Usage): Quota { u.quota }
    public fun usage_period_start_ms(u: &Usage): u64 { u.period_start_ms }
    public fun usage_encoded_bytes_in_period(u: &Usage): u64 { u.encoded_bytes_in_period }
    public fun usage_uploads_in_period(u: &Usage): u64 { u.uploads_in_period }
    public fun usage_write_fees_in_period(u: &Usage): u64 { u.write_fees_in_period }
    public fun usage_total_encoded_bytes(u: &Usage): u64 { u.total_encoded_bytes }
    public fun usage_total_uploads(u: &Usage): u64 { u.total_uploads }

    public fun record_device_id(r: &BlobRecord): Option<ID> { r.device_id }
    public fun record_blob_id(r: &BlobRecord): u256 { r.blob_id }
    public fun record_size(r: &BlobRecord): u64 { r.size }
    public fun record_encoded_size(r: &BlobRecord): u64 { r.encoded_size }
    public fun record_end_epoch(r: &BlobRecord): u32 { r.end_epoch }
    public fun record_uploaded_at_ms(r: &BlobRecord): u64 { r.uploaded_at_ms }

    #[test_only]
    public fun set_version_for_testing(fs: &mut FleetStorage, version: u64) { fs.version = version; }

    /// `refill_pool` as if Walrus were at epoch `now`: the Walrus test system
    /// cannot advance epochs from this package.
    #[test_only]
    public fun refill_pool_at_epoch_for_testing(
        fs: &mut FleetStorage,
        budget: &FleetBudget,
        cap: &BudgetOwnerCap,
        system: &mut System,
        now: u32,
        add_size: u64,
        end_epoch: u32,
        ctx: &mut TxContext,
    ): u64 {
        assert_owner(fs, budget, cap);
        refill(fs, system, now, add_size, end_epoch, ctx)
    }

    #[test_only]
    public fun certify_for_testing(fs: &mut FleetStorage, system: &System, blob_object_id: ID) {
        let blob: &mut Blob = dof::borrow_mut(&mut fs.id, BlobKey { blob_object_id });
        let message = walrus::messages::certified_permanent_blob_message_for_testing(blob.blob_id());
        blob.certify_with_certified_msg_for_testing(system.epoch(), message);
    }
}
