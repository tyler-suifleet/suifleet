#[test_only]
module fleet_storage::fleet_storage_tests {
    use std::string;
    use std::unit_test::destroy;
    use sui::clock::{Self, Clock};
    use sui::coin::{Self, Coin};
    use sui::event;
    use sui::sui::SUI;
    use sui::test_scenario::{Self as ts};
    use fleet_storage::fleet_storage::{Self, FleetStorage, UploadRegistered};
    use sui_edge::device_cap::{Self, DeviceCap};
    use sui_edge::device_registry::{Self, DeviceRegistry, DeviceOwnerCap};
    use sui_edge::fleet_budget::{Self, FleetBudget, BudgetOwnerCap, GasAccount};
    use wal::wal::WAL;
    use walrus::blob::{Self, Blob};
    use walrus::encoding;
    use walrus::storage_resource::Storage;
    use walrus::system::System;

    const OWNER: address = @0xA1;
    const DEVICE: address = @0xD1;
    const DEVICE_2: address = @0xD2;
    const STRANGER: address = @0xBAD;

    const RS2: u8 = 1;
    const DAY: u64 = 86_400_000;
    const POOL: u64 = 100_000_000;
    const WAL_DEPOSIT: u64 = 1_000_000;
    const BLOB: u64 = 1_000;
    const ROOT: u256 = 0xC0FFEE;

    /// Generous quota except for `uploads` per period and `bytes` per period.
    fun quota(bytes: u64, uploads: u64): fleet_storage::Quota {
        fleet_storage::new_quota(DAY, bytes, uploads, 10_000, 1_000_000)
    }

    fun clock_at(scenario: &mut ts::Scenario, now: u64): Clock {
        let mut clock = clock::create_for_testing(ts::ctx(scenario));
        clock.set_for_testing(now);
        clock
    }

    /// Registry; a budget; DEVICE activated and enrolled; Walrus test system
    /// (shared); fleet storage with POOL bytes for 5 epochs and WAL_DEPOSIT.
    fun setup(q: fleet_storage::Quota): (ts::Scenario, System) {
        let mut scenario = ts::begin(OWNER);
        device_registry::init_for_testing(ts::ctx(&mut scenario));
        let mut system = walrus::system::new_for_testing(ts::ctx(&mut scenario));
        ts::next_tx(&mut scenario, OWNER);
        let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
        let limits = fleet_budget::new_limits(10, DAY, 10, 1, 0);
        let budget_cap = fleet_budget::create(&registry, coin::mint_for_testing<SUI>(1_000, ts::ctx(&mut scenario)), limits, 0, ts::ctx(&mut scenario));
        let clock = clock_at(&mut scenario, 1);
        let device = device_registry::register_device(&mut registry, string::utf8(b"pi"), DEVICE, string::utf8(b"aarch64"), &clock, ts::ctx(&mut scenario));
        transfer::public_transfer(device_cap::issue(&device, ts::ctx(&mut scenario)), DEVICE);
        let owner_cap = device_registry::activate_device(&mut registry, device, ts::ctx(&mut scenario));
        clock::destroy_for_testing(clock);
        ts::return_shared(registry);
        transfer::public_transfer(owner_cap, OWNER);
        // Enroll in the shared budget.
        ts::next_tx(&mut scenario, OWNER);
        let mut budget = ts::take_shared<FleetBudget>(&scenario);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let owner_cap = ts::take_from_address<DeviceOwnerCap>(&scenario, OWNER);
        let clock = clock_at(&mut scenario, 1);
        fleet_budget::enroll_device(&mut budget, &budget_cap, &registry, &owner_cap, 0, 0, &clock, ts::ctx(&mut scenario));
        fleet_storage::create(&budget, &budget_cap, q, ts::ctx(&mut scenario));
        clock::destroy_for_testing(clock);
        ts::return_to_address(OWNER, owner_cap);
        ts::return_shared(registry);
        ts::return_shared(budget);
        transfer::public_transfer(budget_cap, OWNER);
        // Buy storage from Walrus and prepay it with WAL.
        ts::next_tx(&mut scenario, OWNER);
        let mut payment = coin::mint_for_testing<WAL>(1_000_000_000, ts::ctx(&mut scenario));
        let storage = system.reserve_space(POOL, 5, &mut payment, ts::ctx(&mut scenario));
        destroy(payment);
        let (mut fs, budget, cap) = owner(&mut scenario);
        fleet_storage::deposit_storage(&mut fs, &budget, &cap, storage);
        fleet_storage::deposit_wal(&mut fs, &budget, &cap, coin::mint_for_testing<WAL>(WAL_DEPOSIT, ts::ctx(&mut scenario)));
        owner_end(fs, budget, cap);
        (scenario, system)
    }

    fun finish(scenario: ts::Scenario, system: System) {
        destroy(system);
        ts::end(scenario);
    }

    fun owner(scenario: &mut ts::Scenario): (FleetStorage, FleetBudget, BudgetOwnerCap) {
        ts::next_tx(scenario, OWNER);
        (ts::take_shared<FleetStorage>(scenario), ts::take_shared<FleetBudget>(scenario), ts::take_from_address<BudgetOwnerCap>(scenario, OWNER))
    }

    fun owner_end(fs: FleetStorage, budget: FleetBudget, cap: BudgetOwnerCap) {
        ts::return_to_address(OWNER, cap);
        ts::return_shared(budget);
        ts::return_shared(fs);
    }

    fun blob_id(size: u64): u256 { blob::derive_blob_id(ROOT, RS2, size) }

    fun encoded(system: &System, size: u64): u64 {
        encoding::encoded_blob_length(size, RS2, system.n_shards())
    }

    /// `sender` registers a blob of `size` with `holder`'s DeviceCap.
    fun upload_as(scenario: &mut ts::Scenario, system: &mut System, sender: address, holder: address, size: u64, now: u64): ID {
        ts::next_tx(scenario, sender);
        let mut fs = ts::take_shared<FleetStorage>(scenario);
        let budget = ts::take_shared<FleetBudget>(scenario);
        let account = ts::take_shared<GasAccount>(scenario);
        let registry = ts::take_shared<DeviceRegistry>(scenario);
        let cap = ts::take_from_address<DeviceCap>(scenario, holder);
        let clock = clock_at(scenario, now);
        let id = fleet_storage::register_blob(&mut fs, &budget, &account, &registry, &cap, system, blob_id(size), ROOT, size, RS2, &clock, ts::ctx(scenario));
        clock::destroy_for_testing(clock);
        ts::return_to_address(holder, cap);
        ts::return_shared(registry);
        ts::return_shared(account);
        ts::return_shared(budget);
        ts::return_shared(fs);
        id
    }

    fun upload(scenario: &mut ts::Scenario, system: &mut System, size: u64, now: u64): ID { upload_as(scenario, system, DEVICE, DEVICE, size, now) }

    fun pool(scenario: &mut ts::Scenario): u64 {
        ts::next_tx(scenario, OWNER);
        let fs = ts::take_shared<FleetStorage>(scenario);
        let value = fleet_storage::pool_size(&fs);
        ts::return_shared(fs);
        value
    }

    fun owner_tx(scenario: &mut ts::Scenario, f: u8, arg: u64) {
        let (mut fs, mut budget, cap) = owner(scenario);
        if (f == 0) fleet_budget::set_paused(&mut budget, &cap, true)
        else if (f == 1) {
            let registry = ts::take_shared<DeviceRegistry>(scenario);
            let owner_cap = ts::take_from_address<DeviceOwnerCap>(scenario, OWNER);
            let mut registry = registry;
            device_registry::revoke_device(&mut registry, &owner_cap);
            ts::return_to_address(OWNER, owner_cap);
            ts::return_shared(registry);
        } else if (f == 2) {
            let mut account = ts::take_shared<GasAccount>(scenario);
            fleet_budget::set_device_suspended(&budget, &cap, &mut account, true);
            ts::return_shared(account);
        } else if (f == 3) fleet_storage::set_version_for_testing(&mut fs, arg)
        else abort 99;
        owner_end(fs, budget, cap);
    }

    // ── Storage splitting and fleet ownership ─────────────────────────────────

    #[test]
    fun an_upload_takes_exactly_its_encoded_size_and_is_fleet_owned() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        let needed = encoded(&system, BLOB);
        let id = upload(&mut scenario, &mut system, BLOB, 10);
        assert!(pool(&mut scenario) == POOL - needed, 0);
        ts::next_tx(&mut scenario, OWNER);
        let fs = ts::take_shared<FleetStorage>(&scenario);
        assert!(fleet_storage::has_blob(&fs, id) && fleet_storage::blob_count(&fs) == 1, 1);
        let b = fleet_storage::blob(&fs, id);
        // Registered non-deletable, with exactly the storage it needs.
        assert!(!b.is_deletable() && b.storage().size() == needed && b.blob_id() == blob_id(BLOB), 2);
        let record = fleet_storage::blob_record(&fs, id).destroy_some();
        assert!(fleet_storage::record_encoded_size(&record) == needed && fleet_storage::record_end_epoch(&record) == 5, 3);
        assert!(fleet_storage::record_device_id(&record).is_some(), 6);
        // The write fee was paid from the fleet's WAL.
        assert!(fleet_storage::wal_balance(&fs) < WAL_DEPOSIT && fleet_storage::total_write_fees(&fs) == WAL_DEPOSIT - fleet_storage::wal_balance(&fs), 4);
        ts::return_shared(fs);
        // Nothing went to the device: it owns no Blob.
        ts::next_tx(&mut scenario, DEVICE);
        assert!(ts::ids_for_address<Blob>(DEVICE).is_empty(), 5);
        finish(scenario, system);
    }

    #[test]
    fun every_upload_emits_an_event() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        ts::next_tx(&mut scenario, DEVICE);
        let mut fs = ts::take_shared<FleetStorage>(&scenario);
        let budget = ts::take_shared<FleetBudget>(&scenario);
        let account = ts::take_shared<GasAccount>(&scenario);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let cap = ts::take_from_address<DeviceCap>(&scenario, DEVICE);
        let clock = clock_at(&mut scenario, 10);
        fleet_storage::register_blob(&mut fs, &budget, &account, &registry, &cap, &mut system, blob_id(BLOB), ROOT, BLOB, RS2, &clock, ts::ctx(&mut scenario));
        assert!(event::events_by_type<UploadRegistered>().length() == 1, 0);
        clock::destroy_for_testing(clock);
        ts::return_to_address(DEVICE, cap);
        ts::return_shared(registry);
        ts::return_shared(account);
        ts::return_shared(budget);
        ts::return_shared(fs);
        finish(scenario, system);
    }

    #[test]
    fun the_whole_pool_can_be_used_exactly() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        let needed = encoded(&system, BLOB);
        // Withdraw all but one blob's worth, then upload that blob.
        let (mut fs, budget, cap) = owner(&mut scenario);
        let rest = fleet_storage::withdraw_storage(&mut fs, &budget, &cap, POOL - needed, ts::ctx(&mut scenario));
        assert!(rest.size() == POOL - needed && fleet_storage::pool_size(&fs) == needed, 0);
        destroy(rest);
        owner_end(fs, budget, cap);
        upload(&mut scenario, &mut system, BLOB, 10);
        assert!(pool(&mut scenario) == 0, 1);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = fleet_storage::EInsufficientStorage)]
    fun uploads_cannot_exceed_the_prepaid_pool() {
        let (mut scenario, mut system) = setup(quota(POOL * 10, 10));
        let needed = encoded(&system, BLOB);
        let (mut fs, budget, cap) = owner(&mut scenario);
        destroy(fleet_storage::withdraw_storage(&mut fs, &budget, &cap, POOL - needed + 1, ts::ctx(&mut scenario)));
        owner_end(fs, budget, cap);
        upload(&mut scenario, &mut system, BLOB, 10);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = walrus::storage_resource::EIncompatibleEpochs)]
    fun deposits_with_another_epoch_range_do_not_fuse() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        ts::next_tx(&mut scenario, OWNER);
        let mut payment = coin::mint_for_testing<WAL>(1_000_000_000, ts::ctx(&mut scenario));
        let other: Storage = system.reserve_space(POOL / 2, 3, &mut payment, ts::ctx(&mut scenario));
        destroy(payment);
        let (mut fs, budget, cap) = owner(&mut scenario);
        fleet_storage::deposit_storage(&mut fs, &budget, &cap, other);
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    // ── Quotas ────────────────────────────────────────────────────────────────

    #[test, expected_failure(abort_code = fleet_storage::EOverQuota)]
    fun encoded_bytes_per_period_are_capped() {
        let (mut scenario, mut system) = setup(quota(1, 10));
        upload(&mut scenario, &mut system, BLOB, 10);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = fleet_storage::ERateLimited)]
    fun uploads_per_period_are_capped() {
        let (mut scenario, mut system) = setup(quota(POOL, 2));
        upload(&mut scenario, &mut system, BLOB, 10);
        upload(&mut scenario, &mut system, BLOB, 11);
        upload(&mut scenario, &mut system, BLOB, 12);
        finish(scenario, system);
    }

    #[test]
    fun a_new_period_restores_the_upload_quota() {
        let (mut scenario, mut system) = setup(quota(POOL, 1));
        upload(&mut scenario, &mut system, BLOB, 10);
        upload(&mut scenario, &mut system, BLOB, 10 + DAY);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = fleet_storage::ETooLarge)]
    fun oversized_blobs_are_refused() {
        let (mut scenario, mut system) = setup(fleet_storage::new_quota(DAY, POOL, 10, BLOB - 1, 1_000_000));
        upload(&mut scenario, &mut system, BLOB, 10);
        finish(scenario, system);
    }

    #[test, expected_failure]
    fun write_fees_are_capped_by_the_quota() {
        // No write fee allowance: Walrus cannot take its write payment.
        let (mut scenario, mut system) = setup(fleet_storage::new_quota(DAY, POOL, 10, 10_000, 0));
        upload(&mut scenario, &mut system, BLOB, 10);
        finish(scenario, system);
    }

    #[test]
    fun a_compromised_device_uploading_at_maximum_rate_is_bounded() {
        let uploads = 3;
        let (mut scenario, mut system) = setup(quota(POOL, uploads));
        let needed = encoded(&system, BLOB);
        let periods = 4;
        let mut p = 0;
        while (p < periods) {
            let mut i = 0;
            while (i < uploads) {
                upload(&mut scenario, &mut system, BLOB, 10 + p * DAY + i);
                i = i + 1;
            };
            p = p + 1;
        };
        assert!(pool(&mut scenario) == POOL - periods * uploads * needed, 0);
        finish(scenario, system);
    }

    // ── Compromised-device controls ───────────────────────────────────────────

    #[test, expected_failure(abort_code = sui_edge::device_cap::EInactiveIdentity)]
    fun a_revoked_device_cannot_upload() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        upload(&mut scenario, &mut system, BLOB, 10);
        owner_tx(&mut scenario, 1, 0);
        upload(&mut scenario, &mut system, BLOB, 11);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = sui_edge::fleet_budget::EPaused)]
    fun a_fleet_wide_pause_stops_uploads() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        owner_tx(&mut scenario, 0, 0);
        upload(&mut scenario, &mut system, BLOB, 10);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = sui_edge::fleet_budget::ESuspended)]
    fun a_suspended_device_cannot_upload() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        owner_tx(&mut scenario, 2, 0);
        upload(&mut scenario, &mut system, BLOB, 10);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = sui_edge::device_cap::EWrongSigner)]
    fun a_stolen_device_cap_cannot_upload() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        upload_as(&mut scenario, &mut system, STRANGER, DEVICE, BLOB, 10);
        finish(scenario, system);
    }

    /// An unenrolled device has no account; the only one there is not its own.
    #[test, expected_failure(abort_code = sui_edge::fleet_budget::EWrongAccount)]
    fun an_unenrolled_device_cannot_upload() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        ts::next_tx(&mut scenario, OWNER);
        let mut registry = ts::take_shared<DeviceRegistry>(&scenario);
        let clock = clock_at(&mut scenario, 1);
        let device = device_registry::register_device(&mut registry, string::utf8(b"pi2"), DEVICE_2, string::utf8(b"aarch64"), &clock, ts::ctx(&mut scenario));
        transfer::public_transfer(device_cap::issue(&device, ts::ctx(&mut scenario)), DEVICE_2);
        let owner_cap = device_registry::activate_device(&mut registry, device, ts::ctx(&mut scenario));
        transfer::public_transfer(owner_cap, OWNER);
        clock::destroy_for_testing(clock);
        ts::return_shared(registry);
        upload_as(&mut scenario, &mut system, DEVICE_2, DEVICE_2, BLOB, 10);
        finish(scenario, system);
    }

    /// The only way a blob leaves fleet custody is the owner's release; a
    /// device holding any budget cap of its own cannot release fleet blobs.
    #[test, expected_failure(abort_code = sui_edge::fleet_budget::ENotOwner)]
    fun a_device_cannot_release_fleet_blobs() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        let id = upload(&mut scenario, &mut system, BLOB, 10);
        ts::next_tx(&mut scenario, DEVICE);
        let mut fs = ts::take_shared<FleetStorage>(&scenario);
        let budget = ts::take_shared<FleetBudget>(&scenario);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let forged = fleet_budget::create(&registry, coin::zero<SUI>(ts::ctx(&mut scenario)), fleet_budget::new_limits(1, 1, 1, 1, 0), 0, ts::ctx(&mut scenario));
        let blob = fleet_storage::release_blob(&mut fs, &budget, &forged, id);
        transfer::public_transfer(blob, DEVICE);
        transfer::public_transfer(forged, DEVICE);
        ts::return_shared(registry);
        ts::return_shared(budget);
        ts::return_shared(fs);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = fleet_storage::EWrongBudget)]
    fun another_fleets_budget_cannot_manage_this_storage() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        ts::next_tx(&mut scenario, STRANGER);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let other_cap = fleet_budget::create(&registry, coin::zero<SUI>(ts::ctx(&mut scenario)), fleet_budget::new_limits(1, 1, 1, 1, 0), 0, ts::ctx(&mut scenario));
        ts::return_shared(registry);
        ts::next_tx(&mut scenario, STRANGER);
        let mut fs = ts::take_shared<FleetStorage>(&scenario);
        let other = ts::take_shared_by_id<FleetBudget>(&scenario, fleet_budget::budget_id(&other_cap));
        transfer::public_transfer(fleet_storage::withdraw_wal(&mut fs, &other, &other_cap, 1, ts::ctx(&mut scenario)), STRANGER);
        ts::return_shared(other);
        ts::return_shared(fs);
        transfer::public_transfer(other_cap, STRANGER);
        finish(scenario, system);
    }

    #[test]
    fun the_owner_releases_a_blob_and_holds_it() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        let id = upload(&mut scenario, &mut system, BLOB, 10);
        let (mut fs, budget, cap) = owner(&mut scenario);
        let blob = fleet_storage::release_blob(&mut fs, &budget, &cap, id);
        assert!(object::id(&blob) == id && !fleet_storage::has_blob(&fs, id) && fleet_storage::blob_count(&fs) == 0, 0);
        assert!(fleet_storage::blob_record(&fs, id).is_none(), 1);
        // The owner may burn it (after expiry) and receive the storage rebate.
        blob.burn();
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    #[test]
    fun the_owner_extends_a_fleet_blob_from_prepaid_wal() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        let id = upload(&mut scenario, &mut system, BLOB, 10);
        // Extension applies to certified blobs; certify with Walrus's test message.
        let (mut fs, budget, cap) = owner(&mut scenario);
        fleet_storage::certify_for_testing(&mut fs, &system, id);
        let before = fleet_storage::wal_balance(&fs);
        fleet_storage::extend_blob(&mut fs, &budget, &cap, &mut system, id, 2, ts::ctx(&mut scenario));
        assert!(fleet_storage::blob(&fs, id).end_epoch() == 7, 0);
        assert!(fleet_storage::record_end_epoch(&fleet_storage::blob_record(&fs, id).destroy_some()) == 7, 1);
        assert!(fleet_storage::wal_balance(&fs) < before, 2);
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = fleet_storage::ESlippage)]
    fun a_swap_below_its_slippage_limit_deposits_nothing() {
        let (mut scenario, system) = setup(quota(POOL, 10));
        let (mut fs, budget, cap) = owner(&mut scenario);
        let swapped = coin::mint_for_testing<WAL>(99, ts::ctx(&mut scenario));
        fleet_storage::deposit_wal_at_least(&mut fs, &budget, &cap, swapped, 100);
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    #[test]
    fun a_swap_meeting_its_slippage_limit_is_deposited() {
        let (mut scenario, system) = setup(quota(POOL, 10));
        let (mut fs, budget, cap) = owner(&mut scenario);
        fleet_storage::deposit_wal_at_least(&mut fs, &budget, &cap, coin::mint_for_testing<WAL>(100, ts::ctx(&mut scenario)), 100);
        assert!(fleet_storage::wal_balance(&fs) == WAL_DEPOSIT + 100, 0);
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    #[test]
    fun the_owner_withdraws_unused_wal() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        let (mut fs, budget, cap) = owner(&mut scenario);
        let coin: Coin<WAL> = fleet_storage::withdraw_wal(&mut fs, &budget, &cap, WAL_DEPOSIT, ts::ctx(&mut scenario));
        assert!(coin.value() == WAL_DEPOSIT && fleet_storage::wal_balance(&fs) == 0, 0);
        destroy(coin);
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    // ── Owner uploads ─────────────────────────────────────────────────────────

    fun owner_upload(scenario: &mut ts::Scenario, system: &mut System, sender: address, cap_holder: address, size: u64): ID {
        ts::next_tx(scenario, sender);
        let mut fs = ts::take_shared<FleetStorage>(scenario);
        let budget = ts::take_shared<FleetBudget>(scenario);
        let cap = ts::take_from_address<BudgetOwnerCap>(scenario, cap_holder);
        let clock = clock_at(scenario, 10);
        let id = fleet_storage::register_fleet_blob(&mut fs, &budget, &cap, system, blob_id(size), ROOT, size, RS2, &clock, ts::ctx(scenario));
        clock::destroy_for_testing(clock);
        ts::return_to_address(cap_holder, cap);
        ts::return_shared(budget);
        ts::return_shared(fs);
        id
    }

    /// The owner's dApp uploads are paid from the same pool and are
    /// fleet-owned and non-deletable, recorded without a device.
    #[test]
    fun an_owner_upload_is_paid_from_the_pool_and_fleet_owned() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        let needed = encoded(&system, BLOB);
        let id = owner_upload(&mut scenario, &mut system, OWNER, OWNER, BLOB);
        let effects = ts::next_tx(&mut scenario, OWNER);
        // Walrus's BlobRegistered and this package's FleetUploadRegistered.
        assert!(ts::num_user_events(&effects) == 2, 4);
        assert!(pool(&mut scenario) == POOL - needed, 0);
        ts::next_tx(&mut scenario, OWNER);
        let fs = ts::take_shared<FleetStorage>(&scenario);
        let b = fleet_storage::blob(&fs, id);
        assert!(!b.is_deletable() && b.storage().size() == needed && b.blob_id() == blob_id(BLOB), 1);
        let record = fleet_storage::blob_record(&fs, id).destroy_some();
        assert!(fleet_storage::record_device_id(&record).is_none() && fleet_storage::record_encoded_size(&record) == needed, 2);
        assert!(fleet_storage::wal_balance(&fs) < WAL_DEPOSIT, 3);
        ts::return_shared(fs);
        assert!(ts::ids_for_address<Blob>(OWNER).is_empty(), 5);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = fleet_storage::EInsufficientStorage)]
    fun owner_uploads_cannot_exceed_the_pool() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        let (mut fs, budget, cap) = owner(&mut scenario);
        destroy(fleet_storage::withdraw_storage(&mut fs, &budget, &cap, POOL - 1, ts::ctx(&mut scenario)));
        owner_end(fs, budget, cap);
        owner_upload(&mut scenario, &mut system, OWNER, OWNER, BLOB);
        finish(scenario, system);
    }

    /// Only this budget's cap registers owner uploads.
    #[test, expected_failure(abort_code = sui_edge::fleet_budget::ENotOwner)]
    fun another_cap_cannot_register_owner_uploads() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        ts::next_tx(&mut scenario, STRANGER);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let forged = fleet_budget::create(&registry, coin::zero<SUI>(ts::ctx(&mut scenario)), fleet_budget::new_limits(1, 1, 1, 1, 0), 0, ts::ctx(&mut scenario));
        ts::return_shared(registry);
        transfer::public_transfer(forged, STRANGER);
        ts::next_tx(&mut scenario, STRANGER);
        let mut fs = ts::take_shared<FleetStorage>(&scenario);
        let budget_id = fleet_storage::budget_id(&fs);
        let budget = ts::take_shared_by_id<FleetBudget>(&scenario, budget_id);
        let cap = ts::take_from_address<BudgetOwnerCap>(&scenario, STRANGER);
        let clock = clock_at(&mut scenario, 10);
        fleet_storage::register_fleet_blob(&mut fs, &budget, &cap, &mut system, blob_id(BLOB), ROOT, BLOB, RS2, &clock, ts::ctx(&mut scenario));
        clock::destroy_for_testing(clock);
        ts::return_to_address(STRANGER, cap);
        ts::return_shared(budget);
        ts::return_shared(fs);
        finish(scenario, system);
    }

    // ── Buying storage with the fleet's WAL ───────────────────────────────────

    fun fund_wal(scenario: &mut ts::Scenario, amount: u64) {
        let (mut fs, budget, cap) = owner(scenario);
        fleet_storage::deposit_wal(&mut fs, &budget, &cap, coin::mint_for_testing<WAL>(amount, ts::ctx(scenario)));
        owner_end(fs, budget, cap);
    }

    /// Bought storage joins the pool and is paid from the fleet's WAL; no WAL
    /// leaves the fleet object.
    #[test]
    fun the_owner_buys_storage_with_fleet_wal() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        fund_wal(&mut scenario, 1_000_000_000);
        let (mut fs, budget, cap) = owner(&mut scenario);
        let before = fleet_storage::wal_balance(&fs);
        let cost = fleet_storage::buy_storage(&mut fs, &budget, &cap, &mut system, POOL, 5, ts::ctx(&mut scenario));
        assert!(cost > 0 && fleet_storage::wal_balance(&fs) == before - cost, 0);
        assert!(fleet_storage::pool_size(&fs) == 2 * POOL && fleet_storage::pool_epochs(&fs).destroy_some() == vector[0, 5], 1);
        owner_end(fs, budget, cap);
        ts::next_tx(&mut scenario, OWNER);
        assert!(ts::ids_for_address<Coin<WAL>>(OWNER).is_empty(), 2);
        finish(scenario, system);
    }

    #[test]
    fun the_owner_extends_the_pool_expiry_with_fleet_wal() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        fund_wal(&mut scenario, 1_000_000_000);
        let (mut fs, budget, cap) = owner(&mut scenario);
        let cost = fleet_storage::extend_pool(&mut fs, &budget, &cap, &mut system, 3, ts::ctx(&mut scenario));
        assert!(cost > 0 && fleet_storage::pool_size(&fs) == POOL, 0);
        assert!(fleet_storage::pool_epochs(&fs).destroy_some() == vector[0, 8], 1);
        owner_end(fs, budget, cap);
        // Uploads now end with the extended pool.
        let id = upload(&mut scenario, &mut system, BLOB, 10);
        ts::next_tx(&mut scenario, OWNER);
        let fs = ts::take_shared<FleetStorage>(&scenario);
        assert!(fleet_storage::blob(&fs, id).end_epoch() == 8, 2);
        ts::return_shared(fs);
        finish(scenario, system);
    }

    /// WAL Walrus charges for `size` bytes over [start, end), from a separate purchase.
    fun price(scenario: &mut ts::Scenario, system: &mut System, size: u64, start: u32, end: u32): u64 {
        let mut payment = coin::mint_for_testing<WAL>(1_000_000_000, ts::ctx(scenario));
        let storage = system.reserve_space_for_epochs(size, start, end, &mut payment, ts::ctx(scenario));
        let cost = 1_000_000_000 - payment.value();
        destroy(storage);
        destroy(payment);
        cost
    }

    /// A partly used pool is topped up and kept longer in one call, in its
    /// first epoch; uploads then end with the refilled pool.
    #[test]
    fun a_partly_used_pool_is_refilled_and_extended() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        fund_wal(&mut scenario, 1_000_000_000);
        let needed = encoded(&system, BLOB);
        upload(&mut scenario, &mut system, BLOB, 10);
        let expected = price(&mut scenario, &mut system, POOL - needed, 5, 8) + price(&mut scenario, &mut system, POOL, 0, 8);
        let (mut fs, budget, cap) = owner(&mut scenario);
        let before = fleet_storage::wal_balance(&fs);
        let cost = fleet_storage::refill_pool(&mut fs, &budget, &cap, &mut system, POOL, 8, ts::ctx(&mut scenario));
        assert!(cost == expected && fleet_storage::wal_balance(&fs) == before - cost, 0);
        assert!(fleet_storage::pool_size(&fs) == 2 * POOL - needed && fleet_storage::pool_epochs(&fs).destroy_some() == vector[0, 8], 1);
        owner_end(fs, budget, cap);
        let refilled = event::events_by_type<fleet_storage::PoolRefilled>();
        assert!(refilled.length() == 1, 2);
        let id = upload(&mut scenario, &mut system, BLOB, DAY + 10);
        ts::next_tx(&mut scenario, OWNER);
        let fs = ts::take_shared<FleetStorage>(&scenario);
        assert!(fleet_storage::blob(&fs, id).end_epoch() == 8 && fleet_storage::pool_size(&fs) == 2 * POOL - 2 * needed, 3);
        ts::return_shared(fs);
        finish(scenario, system);
    }

    /// After the pool's first epoch, Walrus cannot join new storage to it
    /// directly; a refill drops the elapsed epochs, keeps the unused bytes
    /// and adds the new ones over the remaining range.
    #[test]
    fun a_partly_used_pool_is_refilled_after_its_first_epoch() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        fund_wal(&mut scenario, 1_000_000_000);
        let needed = encoded(&system, BLOB);
        upload(&mut scenario, &mut system, BLOB, 10);
        let expected = price(&mut scenario, &mut system, POOL, 2, 5);
        let (mut fs, budget, cap) = owner(&mut scenario);
        let cost = fleet_storage::refill_pool_at_epoch_for_testing(&mut fs, &budget, &cap, &mut system, 2, POOL, 5, ts::ctx(&mut scenario));
        assert!(cost == expected, 0);
        assert!(fleet_storage::pool_size(&fs) == 2 * POOL - needed && fleet_storage::pool_epochs(&fs).destroy_some() == vector[2, 5], 1);
        // Extending later keeps the size and only buys the added epochs.
        let expected = price(&mut scenario, &mut system, 2 * POOL - needed, 5, 9);
        let cost = fleet_storage::refill_pool_at_epoch_for_testing(&mut fs, &budget, &cap, &mut system, 3, 0, 9, ts::ctx(&mut scenario));
        assert!(cost == expected, 2);
        assert!(fleet_storage::pool_size(&fs) == 2 * POOL - needed && fleet_storage::pool_epochs(&fs).destroy_some() == vector[3, 9], 3);
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    /// An expired pool, with unused bytes left, is replaced by fresh storage.
    #[test]
    fun an_expired_pool_is_replaced_by_a_refill() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        fund_wal(&mut scenario, 1_000_000_000);
        upload(&mut scenario, &mut system, BLOB, 10);
        let expected = price(&mut scenario, &mut system, POOL, 6, 9);
        let (mut fs, budget, cap) = owner(&mut scenario);
        let cost = fleet_storage::refill_pool_at_epoch_for_testing(&mut fs, &budget, &cap, &mut system, 6, POOL, 9, ts::ctx(&mut scenario));
        assert!(cost == expected, 0);
        assert!(fleet_storage::pool_size(&fs) == POOL && fleet_storage::pool_epochs(&fs).destroy_some() == vector[6, 9], 1);
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = fleet_storage::EInvalidEpochs)]
    fun a_refill_cannot_shorten_the_pool() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        fund_wal(&mut scenario, 1_000_000_000);
        let (mut fs, budget, cap) = owner(&mut scenario);
        fleet_storage::refill_pool(&mut fs, &budget, &cap, &mut system, POOL, 3, ts::ctx(&mut scenario));
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = fleet_storage::EInvalidEpochs)]
    fun a_refill_must_end_after_the_current_epoch() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        fund_wal(&mut scenario, 1_000_000_000);
        let (mut fs, budget, cap) = owner(&mut scenario);
        fleet_storage::refill_pool_at_epoch_for_testing(&mut fs, &budget, &cap, &mut system, 6, POOL, 6, ts::ctx(&mut scenario));
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    /// Refilling an expired pool with nothing to add would leave no storage.
    #[test, expected_failure(abort_code = fleet_storage::EZeroAmount)]
    fun an_expired_pool_needs_new_storage() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        let (mut fs, budget, cap) = owner(&mut scenario);
        fleet_storage::refill_pool_at_epoch_for_testing(&mut fs, &budget, &cap, &mut system, 6, 0, 9, ts::ctx(&mut scenario));
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = sui_edge::fleet_budget::ENotOwner)]
    fun only_the_owner_refills_the_pool() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        ts::next_tx(&mut scenario, STRANGER);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let forged = fleet_budget::create(&registry, coin::zero<SUI>(ts::ctx(&mut scenario)), fleet_budget::new_limits(1, 1, 1, 1, 0), 0, ts::ctx(&mut scenario));
        ts::return_shared(registry);
        ts::next_tx(&mut scenario, STRANGER);
        let mut fs = ts::take_shared<FleetStorage>(&scenario);
        let budget = ts::take_shared_by_id<FleetBudget>(&scenario, fleet_storage::budget_id(&fs));
        fleet_storage::refill_pool(&mut fs, &budget, &forged, &mut system, 1, 9, ts::ctx(&mut scenario));
        transfer::public_transfer(forged, STRANGER);
        ts::return_shared(budget);
        ts::return_shared(fs);
        finish(scenario, system);
    }

    /// Without enough fleet WAL, Walrus refuses the purchase and nothing changes.
    #[test, expected_failure]
    fun buying_storage_needs_enough_fleet_wal() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        let (mut fs, budget, cap) = owner(&mut scenario);
        fleet_storage::buy_storage(&mut fs, &budget, &cap, &mut system, POOL * 1_000, 50, ts::ctx(&mut scenario));
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    #[test, expected_failure(abort_code = sui_edge::fleet_budget::ENotOwner)]
    fun only_the_owner_buys_storage() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        ts::next_tx(&mut scenario, STRANGER);
        let registry = ts::take_shared<DeviceRegistry>(&scenario);
        let forged = fleet_budget::create(&registry, coin::zero<SUI>(ts::ctx(&mut scenario)), fleet_budget::new_limits(1, 1, 1, 1, 0), 0, ts::ctx(&mut scenario));
        ts::return_shared(registry);
        ts::next_tx(&mut scenario, STRANGER);
        let mut fs = ts::take_shared<FleetStorage>(&scenario);
        let budget = ts::take_shared_by_id<FleetBudget>(&scenario, fleet_storage::budget_id(&fs));
        fleet_storage::buy_storage(&mut fs, &budget, &forged, &mut system, 1, 1, ts::ctx(&mut scenario));
        transfer::public_transfer(forged, STRANGER);
        ts::return_shared(budget);
        ts::return_shared(fs);
        finish(scenario, system);
    }

    // ── Package versions ──────────────────────────────────────────────────────

    #[test, expected_failure(abort_code = fleet_storage::EWrongVersion)]
    fun an_older_package_version_cannot_upload_after_migration() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        owner_tx(&mut scenario, 3, fleet_storage::package_version() + 1);
        upload(&mut scenario, &mut system, BLOB, 10);
        finish(scenario, system);
    }

    #[test]
    fun reads_report_the_pool_expiry_and_usage() {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        let needed = encoded(&system, BLOB);
        upload(&mut scenario, &mut system, BLOB, 10);
        ts::next_tx(&mut scenario, DEVICE);
        let cap = ts::take_from_address<DeviceCap>(&scenario, DEVICE);
        let device_id = device_cap::device_id(&cap);
        ts::return_to_address(DEVICE, cap);
        ts::next_tx(&mut scenario, OWNER);
        let fs = ts::take_shared<FleetStorage>(&scenario);
        assert!(fleet_storage::pool_epochs(&fs).destroy_some() == vector[0, 5], 0);
        let usage = fleet_storage::usage(&fs, device_id).destroy_some();
        assert!(fleet_storage::usage_encoded_bytes_in_period(&usage) == needed && fleet_storage::usage_total_uploads(&usage) == 1, 1);
        assert!(fleet_storage::interface_version() == 2, 2);
        ts::return_shared(fs);
        finish(scenario, system);
    }
    #[test]
    fun reserves_are_isolated_and_alternate_funding_grants_no_authority() {
        let (mut scenario, system) = setup(quota(POOL, 10));
        let (mut fs, budget, cap) = owner(&mut scenario);
        fleet_storage::configure_retention(&mut fs, &budget, &cap, DEVICE_2, 2, 100, 10, 5);
        owner_end(fs, budget, cap);
        ts::next_tx(&mut scenario, STRANGER);
        let mut fs = ts::take_shared<FleetStorage>(&scenario);
        let ctx = ts::ctx(&mut scenario);
        fleet_storage::fund_retention(&mut fs, coin::mint_for_testing<WAL>(100, ctx), coin::mint_for_testing<SUI>(10, ctx));
        let (wal, gas) = fleet_storage::retention_balances(&fs);
        assert!(wal == 100 && gas == 10);
        assert!(fleet_storage::wal_balance(&fs) == WAL_DEPOSIT);
        ts::return_shared(fs);
        let (mut fs, budget, cap) = owner(&mut scenario);
        let ctx = ts::ctx(&mut scenario);
        destroy(fleet_storage::withdraw_wal(&mut fs, &budget, &cap, WAL_DEPOSIT, ctx));
        let (wal, gas) = fleet_storage::retention_balances(&fs);
        assert!(wal == 100 && gas == 10);
        let (wal, gas) = fleet_storage::withdraw_retention(&mut fs, &budget, &cap, 100, 10, ctx);
        destroy(wal);
        destroy(gas);
        let (wal, gas) = fleet_storage::retention_balances(&fs);
        assert!(wal == 0 && gas == 0);
        assert!(event::events_by_type<fleet_storage::RetentionRisk>().length() == 1);
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    #[test]
    #[expected_failure(abort_code = 0, location = fleet_storage)]
    fun retention_rejects_stale_object_version() {
        let (mut scenario, system) = setup(quota(POOL, 10));
        let (mut fs, budget, cap) = owner(&mut scenario);
        fleet_storage::set_version_for_testing(&mut fs, 1);
        fleet_storage::configure_retention(&mut fs, &budget, &cap, DEVICE_2, 2, 100, 10, 5);
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    fun renewal_fixture(): (ts::Scenario, System, ID) {
        let (mut scenario, mut system) = setup(quota(POOL, 10));
        let id = upload(&mut scenario, &mut system, BLOB, 10);
        let (mut fs, budget, cap) = owner(&mut scenario);
        fleet_storage::certify_for_testing(&mut fs, &system, id);
        fleet_storage::configure_retention(&mut fs, &budget, &cap, DEVICE_2, 5, 1_000_000, 10, 5);
        fleet_storage::authorize_renewal(&mut fs, &budget, &cap, id, 7, 1_000_000);
        let ctx = ts::ctx(&mut scenario);
        fleet_storage::fund_retention(&mut fs, coin::mint_for_testing<WAL>(1_000_000, ctx), coin::mint_for_testing<SUI>(10, ctx));
        owner_end(fs, budget, cap);
        (scenario, system, id)
    }

    #[test]
    fun restricted_executor_extends_and_accounts_real_walrus_cost() {
        let (mut scenario, mut system, id) = renewal_fixture();
        ts::next_tx(&mut scenario, DEVICE_2);
        let mut fs = ts::take_shared<FleetStorage>(&scenario);
        fleet_storage::execute_renewal(&mut fs, &mut system, id, 5, 1, ts::ctx(&mut scenario));
        assert!(fleet_storage::blob(&fs, id).end_epoch() == 7);
        let (wal, gas) = fleet_storage::retention_balances(&fs);
        assert!(wal < 1_000_000 && gas == 5);
        assert!(fleet_storage::wal_balance(&fs) < WAL_DEPOSIT);
        assert!(event::events_by_type<fleet_storage::RenewalCompleted>().length() == 1);
        ts::return_shared(fs);
        finish(scenario, system);
    }

    #[test]
    #[expected_failure(abort_code = 1, location = fleet_storage)]
    fun stranger_cannot_execute() {
        let (mut scenario, mut system, id) = renewal_fixture();
        ts::next_tx(&mut scenario, STRANGER);
        let mut fs = ts::take_shared<FleetStorage>(&scenario);
        fleet_storage::execute_renewal(&mut fs, &mut system, id, 5, 1, ts::ctx(&mut scenario));
        ts::return_shared(fs);
        finish(scenario, system);
    }

    #[test]
    #[expected_failure(abort_code = 13, location = fleet_storage)]
    fun duplicate_cannot_spend_again() {
        let (mut scenario, mut system, id) = renewal_fixture();
        ts::next_tx(&mut scenario, DEVICE_2);
        let mut fs = ts::take_shared<FleetStorage>(&scenario);
        fleet_storage::execute_renewal(&mut fs, &mut system, id, 5, 1, ts::ctx(&mut scenario));
        fleet_storage::execute_renewal(&mut fs, &mut system, id, 5, 1, ts::ctx(&mut scenario));
        ts::return_shared(fs);
        finish(scenario, system);
    }

    #[test]
    fun retained_owner_registration_reserves_complete_closure_atomically() {
        let (mut scenario, mut system, _) = renewal_fixture();
        let (mut fs, budget, cap) = owner(&mut scenario);
        let ctx = ts::ctx(&mut scenario);
        fleet_storage::fund_retention(&mut fs, coin::mint_for_testing<WAL>(1_000_000, ctx), coin::mint_for_testing<SUI>(0, ctx));
        let clock = clock_at(&mut scenario, 20);
        let id = fleet_storage::register_retained_fleet_blob(&mut fs, &budget, &cap, &mut system,
            blob_id(BLOB + 1), ROOT, BLOB + 1, RS2, &clock, 7, 1_000_000, ts::ctx(&mut scenario));
        assert!(fleet_storage::has_blob(&fs, id));
        assert!(fleet_storage::renewal(&fs, id).is_some());
        clock::destroy_for_testing(clock);
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

    #[test]
    #[expected_failure(abort_code = 7, location = fleet_storage)]
    fun legacy_upload_cannot_create_unreserved_blob() {
        let (mut scenario, mut system, _) = renewal_fixture();
        upload(&mut scenario, &mut system, BLOB + 1, 20);
        finish(scenario, system);
    }

    #[test]
    #[expected_failure(abort_code = 7, location = fleet_storage)]
    fun owner_withdrawal_pauses_discretionary_spending() {
        let (mut scenario, mut system, _) = renewal_fixture();
        let (mut fs, budget, cap) = owner(&mut scenario);
        let (wal, gas) = fleet_storage::withdraw_retention(&mut fs, &budget, &cap, 1, 0, ts::ctx(&mut scenario));
        destroy(wal); destroy(gas);
        fleet_storage::buy_storage(&mut fs, &budget, &cap, &mut system, 1, 5, ts::ctx(&mut scenario));
        owner_end(fs, budget, cap);
        finish(scenario, system);
    }

}
