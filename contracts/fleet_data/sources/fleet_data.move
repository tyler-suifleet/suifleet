/// SF-082 authenticated fleet data. This package consumes SF-083 interface v1;
/// it does not change the budget or storage packages. Device writes read the
/// fleet policy and mutate only their own DeviceData. Logs remain ciphertext.
module fleet_data::fleet_data {
    use std::string::String;
    use sui::bcs;
    use sui::clock::Clock;
    use sui::dynamic_field as df;
    use sui::event;
    use sui_edge::customer::{Self, Customer, Fleet};
    use sui_edge::device_cap::{Self, DeviceCap};
    use sui_edge::device_registry::DeviceRegistry;
    use sui_edge::fleet_budget::{Self, FleetBudget, BudgetOwnerCap, GasAccount};
    use fleet_storage::fleet_storage::{Self, FleetStorage};

    const VERSION: u64 = 1;
    const EBinding: u64 = 0;
    const EVersion: u64 = 1;
    const ESequence: u64 = 2;
    const ECadence: u64 = 3;
    const EBounds: u64 = 4;
    const EBlob: u64 = 5;
    const EAccess: u64 = 6;
    const EExists: u64 = 7;

    public struct FleetData has key {
        id: UID,
        version: u64,
        budget_id: ID,
        storage_id: ID,
        registry_id: ID,
        chain_identifier: String,
        key_epoch: u64,
        support: vector<address>,
        health_interval_ms: u64,
        batch_interval_ms: u64,
        emergency_interval_ms: u64,
        device_count: u64,
    }
    /// The fleet's dynamic-field index is enumerable with ListDynamicFields.
    public struct DeviceKey has copy, drop, store { device_id: ID }
    public struct DeviceData has key {
        id: UID,
        version: u64,
        fleet_id: ID,
        device_id: ID,
        account_id: ID,
        health_seq: u64,
        health_at_ms: u64,
        message: vector<u8>,
        signature: vector<u8>,
        batch_seq: u64,
        batch_at_ms: Option<u64>,
        emergency_at_ms: Option<u64>,
        anchor_count: u64,
    }
    public struct AnchorKey has copy, drop, store { index: u64 }
    public struct Anchor has copy, drop, store {
        seq: u64,
        kind: u8,
        blob_object_id: ID,
        blob_id: u256,
        ciphertext_sha256: vector<u8>,
        signer: address,
        at_ms: u64,
        end_epoch: u32,
        key_epoch: u64,
    }
    /// Bounded current root. Historical heads are carried inside signed,
    /// encrypted archives, committed by this hash chain, not repeated on Sui.
    /// These types have their own introduction-package origin on upgrades.
    public struct CompactHeadKey has copy, drop, store { reserved: bool }
    public struct CompactHead has copy, drop, store {
        first_index: u64,
        index: u64,
        anchor: Anchor,
        previous_hash: vector<u8>,
    }

    public struct HealthUpdated has copy, drop {
        fleet_id: ID, device_id: ID, seq: u64, at_ms: u64,
    }
    public struct DataAnchored has copy, drop {
        fleet_id: ID, device_id: ID, index: u64, anchor: Anchor,
    }

    public fun create(budget: &FleetBudget, cap: &BudgetOwnerCap,
        storage: &FleetStorage, chain_identifier: String, ctx: &mut TxContext) {
        fleet_budget::assert_owner(budget, cap);
        assert!(fleet_storage::budget_id(storage) == object::id(budget), EBinding);
        assert!(chain_identifier.as_bytes().length() == 8, EBounds);
        transfer::share_object(FleetData {
            id: object::new(ctx), version: VERSION, budget_id: object::id(budget),
            storage_id: object::id(storage), registry_id: fleet_budget::registry_id(budget),
            chain_identifier, key_epoch: 0, support: vector[],
            health_interval_ms: 86_400_000, batch_interval_ms: 86_400_000,
            emergency_interval_ms: 300_000, device_count: 0,
        });
    }

    /// Owner enrollment prevents a device from resetting cadence by creating
    /// another writer. Re-enrollment after account replacement is explicit.
    public fun enroll(fleet: &mut FleetData, budget: &FleetBudget,
        cap: &BudgetOwnerCap, account: &GasAccount, ctx: &mut TxContext) {
        owner(fleet, budget, cap);
        assert!(fleet_budget::account_budget_id(account) == fleet.budget_id, EBinding);
        let device_id = fleet_budget::account_device_id(account);
        assert!(fleet_budget::account_id(budget, device_id) == option::some(object::id(account)), EBinding);
        let key = DeviceKey { device_id };
        assert!(!df::exists(&fleet.id, key), EExists);
        let writer = DeviceData {
            id: object::new(ctx), version: VERSION, fleet_id: object::id(fleet),
            device_id, account_id: object::id(account), health_seq: 0,
            health_at_ms: 0, message: vector[], signature: vector[], batch_seq: 0,
            batch_at_ms: option::none(), emergency_at_ms: option::none(), anchor_count: 0,
        };
        df::add(&mut fleet.id, key, object::id(&writer));
        fleet.device_count = fleet.device_count + 1;
        transfer::share_object(writer);
    }

    /// Explicit owner migration after SF-083 replaces a GasAccount. Preserve
    /// the complete history, sequences and cadence; devices cannot reset them.
    public fun replace_account(fleet: &FleetData, writer: &mut DeviceData,
        budget: &FleetBudget, cap: &BudgetOwnerCap, account: &GasAccount) {
        owner(fleet, budget, cap);
        assert!(writer.version == VERSION && writer.fleet_id == object::id(fleet), EBinding);
        assert!(*df::borrow<DeviceKey, ID>(&fleet.id, DeviceKey { device_id: writer.device_id }) == object::id(writer), EBinding);
        assert!(fleet_budget::account_budget_id(account) == fleet.budget_id
            && fleet_budget::account_device_id(account) == writer.device_id, EBinding);
        assert!(fleet_budget::account_id(budget, writer.device_id) == option::some(object::id(account)), EBinding);
        writer.account_id = object::id(account);
    }

    public fun configure(fleet: &mut FleetData, budget: &FleetBudget,
        cap: &BudgetOwnerCap, health_interval_ms: u64, batch_interval_ms: u64,
        emergency_interval_ms: u64) {
        owner(fleet, budget, cap);
        assert!(health_interval_ms >= 60_000 && health_interval_ms <= 86_400_000, EBounds);
        assert!(batch_interval_ms >= 3_600_000 && batch_interval_ms <= 604_800_000, EBounds);
        assert!(emergency_interval_ms >= 60_000 && emergency_interval_ms <= batch_interval_ms, EBounds);
        fleet.health_interval_ms = health_interval_ms;
        fleet.batch_interval_ms = batch_interval_ms;
        fleet.emergency_interval_ms = emergency_interval_ms;
    }

    public struct DiagnosticKey has copy, drop, store {}
    public struct DiagnosticRequest has copy, drop, store { seq: u64, at_ms: u64 }

    /// Owner/support requests are chain records; the writer observes them and
    /// uploads a bounded encrypted diagnostic batch under the emergency quota.
    public fun request_diagnostics(fleet: &FleetData, writer: &mut DeviceData,
        budget: &FleetBudget, cap: &BudgetOwnerCap, clock: &Clock) {
        owner(fleet, budget, cap);
        request(fleet, writer, clock);
    }
    #[allow(lint(prefer_mut_tx_context))]
    public fun request_diagnostics_support(fleet: &FleetData, writer: &mut DeviceData,
        clock: &Clock, ctx: &TxContext) {
        assert!(fleet.version == VERSION && fleet.support.contains(&ctx.sender()), EAccess);
        request(fleet, writer, clock);
    }
    fun request(fleet: &FleetData, writer: &mut DeviceData, clock: &Clock) {
        assert!(writer.version == VERSION && writer.fleet_id == object::id(fleet), EBinding);
        assert!(*df::borrow<DeviceKey, ID>(&fleet.id, DeviceKey { device_id: writer.device_id }) == object::id(writer), EBinding);
        let now = clock.timestamp_ms();
        if (df::exists(&writer.id, DiagnosticKey {})) {
            let current: &mut DiagnosticRequest = df::borrow_mut(&mut writer.id, DiagnosticKey {});
            assert!(now >= current.at_ms + fleet.emergency_interval_ms, ECadence);
            current.seq = current.seq + 1;
            current.at_ms = now;
        } else { df::add(&mut writer.id, DiagnosticKey {}, DiagnosticRequest { seq: 1, at_ms: now }); };
    }
    public fun diagnostic_request(writer: &DeviceData): Option<DiagnosticRequest> {
        if (df::exists(&writer.id, DiagnosticKey {})) option::some(*df::borrow(&writer.id, DiagnosticKey {})) else option::none()
    }
    public fun diagnostic_seq(request: &DiagnosticRequest): u64 { request.seq }

    public fun set_support(fleet: &mut FleetData, budget: &FleetBudget,
        cap: &BudgetOwnerCap, account: address, allowed: bool) {
        owner(fleet, budget, cap);
        let (found, index) = fleet.support.index_of(&account);
        if (allowed && !found) {
            assert!(fleet.support.length() < 64, EBounds);
            fleet.support.push_back(account);
        } else if (!allowed && found) { fleet.support.swap_remove(index); };
    }

    public fun rotate_key_epoch(fleet: &mut FleetData, budget: &FleetBudget, cap: &BudgetOwnerCap) {
        owner(fleet, budget, cap);
        fleet.key_epoch = fleet.key_epoch + 1;
    }

    /// Compact signed snapshot, directly on chain, never a Walrus blob. The
    /// browser verifies the personal-message signature and JSON bindings; chain
    /// authorization alone does not make malformed device claims trustworthy.
    #[allow(lint(prefer_mut_tx_context))]
    public fun report_health(fleet: &FleetData, writer: &mut DeviceData,
        budget: &FleetBudget, account: &GasAccount, registry: &DeviceRegistry,
        cap: &DeviceCap, seq: u64, message: vector<u8>, signature: vector<u8>,
        clock: &Clock, ctx: &TxContext) {
        device(fleet, writer, budget, account, registry, cap, ctx);
        assert!(seq > writer.health_seq, ESequence);
        assert!(!message.is_empty() && message.length() <= 16_384, EBounds);
        assert!(signature.length() == 97 || signature.length() == 98, EBounds);
        let now = clock.timestamp_ms();
        if (writer.health_seq > 0) assert!(now >= writer.health_at_ms + fleet.health_interval_ms, ECadence);
        writer.health_seq = seq;
        writer.health_at_ms = now;
        writer.message = message;
        writer.signature = signature;
        event::emit(HealthUpdated { fleet_id: object::id(fleet), device_id: writer.device_id, seq, at_ms: now });
    }

    /// Anchor only a certified, non-deletable blob paid for by this fleet and
    /// registered by this device. The caller must register through SF-083 and
    /// certify first. Kind 0 = periodic logs, 1 = diagnostics, 2 = crash.
    /// Emergency uploads share a bounded rate even when a device lies about
    /// the reason; SF-083 byte/upload/fee quotas are the final spending bound.
    #[allow(lint(prefer_mut_tx_context))]
    public fun anchor(fleet: &FleetData, writer: &mut DeviceData,
        budget: &FleetBudget, account: &GasAccount, registry: &DeviceRegistry,
        cap: &DeviceCap, storage: &FleetStorage, blob_object_id: ID,
        seq: u64, kind: u8, ciphertext_sha256: vector<u8>, clock: &Clock, ctx: &TxContext) {
        anchor_impl(fleet, writer, budget, account, registry, cap, storage,
            blob_object_id, seq, kind, ciphertext_sha256, false, vector[], clock, ctx);
    }

    /// Opt-in bounded index. The archive contains the complete previous head
    /// bytes; readers authenticate that link against previous_hash. No mutable
    /// gateway/indexer is trusted and no archive contents are put on chain.
    public fun anchor_compact(fleet: &FleetData, writer: &mut DeviceData,
        budget: &FleetBudget, account: &GasAccount, registry: &DeviceRegistry,
        cap: &DeviceCap, storage: &FleetStorage, blob_object_id: ID,
        seq: u64, kind: u8, ciphertext_sha256: vector<u8>, expected_previous: vector<u8>,
        clock: &Clock, ctx: &mut TxContext) {
        assert!(expected_previous.is_empty() || expected_previous.length() == 32, EBounds);
        anchor_impl(fleet, writer, budget, account, registry, cap, storage,
            blob_object_id, seq, kind, ciphertext_sha256, true, expected_previous, clock, ctx);
    }

    public fun compact_head(writer: &DeviceData): Option<CompactHead> {
        let key = CompactHeadKey { reserved: false };
        if (df::exists(&writer.id, key)) option::some(*df::borrow(&writer.id, key)) else option::none()
    }

    fun anchor_impl(fleet: &FleetData, writer: &mut DeviceData,
        budget: &FleetBudget, account: &GasAccount, registry: &DeviceRegistry,
        cap: &DeviceCap, storage: &FleetStorage, blob_object_id: ID,
        seq: u64, kind: u8, ciphertext_sha256: vector<u8>, compact: bool, expected_previous: vector<u8>, clock: &Clock, ctx: &TxContext) {
        device(fleet, writer, budget, account, registry, cap, ctx);
        assert!(object::id(storage) == fleet.storage_id, EBinding);
        assert!(seq > writer.batch_seq, ESequence);
        assert!(kind <= 2 && ciphertext_sha256.length() == 32, EBounds);
        let record = fleet_storage::blob_record(storage, blob_object_id).destroy_some();
        assert!(fleet_storage::record_device_id(&record) == option::some(writer.device_id), EBlob);
        let blob = fleet_storage::blob(storage, blob_object_id);
        assert!(!blob.is_deletable() && blob.certified_epoch().is_some(), EBlob);
        let now = clock.timestamp_ms();
        // Legacy entries retain per-blob markers. Compact history only rejects
        // reuse of its current head below; it retains no unbounded seen-blob set.
        // Readers independently check ciphertext sequence bindings on every link.
        assert!(!df::exists(&writer.id, blob_object_id), EExists);
        if (kind == 0) {
            if (writer.batch_at_ms.is_some()) assert!(now >= *writer.batch_at_ms.borrow() + fleet.batch_interval_ms, ECadence);
            writer.batch_at_ms = option::some(now);
        } else {
            if (writer.emergency_at_ms.is_some()) assert!(now >= *writer.emergency_at_ms.borrow() + fleet.emergency_interval_ms, ECadence);
            writer.emergency_at_ms = option::some(now);
        };
        let anchor = Anchor { seq, kind, blob_object_id,
            blob_id: fleet_storage::record_blob_id(&record), ciphertext_sha256,
            signer: ctx.sender(), at_ms: now,
            end_epoch: fleet_storage::record_end_epoch(&record), key_epoch: fleet.key_epoch };
        let index = writer.anchor_count;
        if (compact) {
            let key = CompactHeadKey { reserved: false };
            let first_index;
            let previous_hash;
            if (df::exists(&writer.id, key)) {
                let old: CompactHead = df::remove(&mut writer.id, key);
                assert!(old.anchor.blob_object_id != blob_object_id, EExists);
                first_index = old.first_index;
                previous_hash = std::hash::sha2_256(bcs::to_bytes(&old));
            } else {
                first_index = index;
                previous_hash = vector[];
            };
            // Compare-and-swap binds the encrypted archive's predecessor to
            // the actual prior chain head, including across signer rotation.
            assert!(expected_previous == previous_hash, ESequence);
            df::add(&mut writer.id, key, CompactHead { first_index, index, anchor, previous_hash });
        } else {
            df::add(&mut writer.id, AnchorKey { index }, anchor);
            df::add(&mut writer.id, blob_object_id, true);
        };
        writer.anchor_count = index + 1;
        writer.batch_seq = seq;
        event::emit(DataAnchored { fleet_id: object::id(fleet), device_id: writer.device_id, index, anchor });
    }

    /// SF139 additive scoped index. Original FleetData/DeviceData layouts stay stable.
    public struct ItemKey has copy, drop, store { batch: u64, item: u64 }
    public struct DataItem has copy, drop, store {
        anchor_index: u64, sequence: u64, stream: String, key: String, content_type: String,
        metadata: vector<u8>, upload: String, chunk: u32, chunks: u32, total_size: u64,
        size: u64, sha256: vector<u8>, identifier: String, ciphertext_size: u64,
        ciphertext_sha256: vector<u8>,
    }
    public struct ReadGrantKey has copy, drop, store { device_id: ID, stream_hash: vector<u8>, reader: address }
    public fun set_read_grant(fleet: &mut FleetData, budget: &FleetBudget, cap: &BudgetOwnerCap,
        device_id: ID, stream_hash: vector<u8>, reader: address, allowed: bool) {
        owner(fleet, budget, cap);
        assert!(writer_id(fleet, device_id).is_some() && (stream_hash.is_empty() || stream_hash.length() == 32), EBounds);
        let key = ReadGrantKey {device_id, stream_hash, reader};
        if (df::exists(&fleet.id, key)) { let _: bool = df::remove(&mut fleet.id, key); };
        if (allowed) df::add(&mut fleet.id, key, true);
    }
    /// Exactly120 bytes. Every legacy approval (including old packages) refuses >104.
    public fun scoped_identity_matches(fleet: &FleetData, id: &vector<u8>, device_id: ID, stream_hash: &vector<u8>): bool {
        assert!(fleet.version == VERSION, EVersion);
        if (id.length() != 120 || stream_hash.length() != 32 || writer_id(fleet, device_id).is_none()) return false;
        let mut prefix = identity_prefix(fleet);
        prefix.append(device_id.to_bytes()); prefix.append(*stream_hash);
        let mut i = 0;
        while (i < 104) { if (id[i] != prefix[i]) return false; i = i + 1; };
        true
    }
    entry fun seal_approve_scoped_owner(id: vector<u8>, fleet: &FleetData,
        budget: &FleetBudget, cap: &BudgetOwnerCap, device_id: ID, stream_hash: vector<u8>) {
        owner(fleet, budget, cap);
        assert!(scoped_identity_matches(fleet, &id, device_id, &stream_hash), EAccess);
    }
    /// Read-only managed authority; credential revocation is checked on every request.
    entry fun seal_approve_scoped_customer(id: vector<u8>, fleet: &FleetData,
        budget: &FleetBudget, customer: &Customer, managed: &Fleet,
        device_id: ID, stream_hash: vector<u8>, ctx: &mut TxContext) {
        customer::assert_admin(customer, ctx);
        customer::assert_fleet(customer, managed);
        assert!(fleet.version == VERSION && object::id(budget) == fleet.budget_id
            && customer::fleet_budget(managed) == fleet.budget_id, EBinding);
        assert!(scoped_identity_matches(fleet, &id, device_id, &stream_hash), EAccess);
    }
    entry fun seal_approve_scoped_reader(id: vector<u8>, fleet: &FleetData,
        device_id: ID, stream_hash: vector<u8>, ctx: &TxContext) {
        assert!(scoped_identity_matches(fleet, &id, device_id, &stream_hash), EAccess);
        assert!(df::exists(&fleet.id, ReadGrantKey {device_id, stream_hash, reader:ctx.sender()})
            || df::exists(&fleet.id, ReadGrantKey {device_id, stream_hash:vector[], reader:ctx.sender()}), EAccess);
    }
    /// One anchor transaction indexes up to16 independent opaque patches.
    public fun anchor_items(fleet: &FleetData, writer: &mut DeviceData,
        budget: &FleetBudget, account: &GasAccount, registry: &DeviceRegistry,
        cap: &DeviceCap, storage: &FleetStorage, blob_object_id: ID,
        seq: u64, kind: u8, ciphertext_sha256: vector<u8>, items: vector<vector<u8>>,
        clock: &Clock, ctx: &mut TxContext) {
        assert!(!items.is_empty() && items.length() <= 16, EBounds);
        let anchor_index = writer.anchor_count;
        anchor_impl(fleet, writer, budget, account, registry, cap, storage,
            blob_object_id, seq, kind, ciphertext_sha256, false, vector[], clock, ctx);
        let mut n = 0;
        while (n < items.length()) {
            let mut bytes = bcs::new(items[n]);
            let sequence = bytes.peel_u64();
            let stream = std::string::utf8(bytes.peel_vec_u8());
            let key = std::string::utf8(bytes.peel_vec_u8());
            let content_type = std::string::utf8(bytes.peel_vec_u8());
            let metadata = bytes.peel_vec_u8();
            let upload = std::string::utf8(bytes.peel_vec_u8());
            let chunk = bytes.peel_u32(); let chunks = bytes.peel_u32();
            let total_size = bytes.peel_u64(); let size = bytes.peel_u64();
            let sha256 = bytes.peel_vec_u8(); let identifier = std::string::utf8(bytes.peel_vec_u8());
            let ciphertext_size = bytes.peel_u64(); let ciphertext_sha256 = bytes.peel_vec_u8();
            assert!(bytes.into_remainder_bytes().is_empty(), EBounds);
            assert!(stream.length() > 0 && stream.length() <= 128 && key.length() > 0 && key.length() <= 256
                && content_type.length() > 0 && content_type.length() <= 128 && metadata.length() <= 6144
                && upload.length() == 64 && chunks > 0 && chunks <= 2731 && chunk < chunks
                && total_size <= 67108864 && size <= 24576 && size <= total_size && sha256.length() == 32
                && identifier.length() > 0 && identifier.length() <= 64
                && ciphertext_size > 0 && ciphertext_size <= 131072 && ciphertext_sha256.length() == 32, EBounds);
            df::add(&mut writer.id, ItemKey {batch:seq,item:n}, DataItem {anchor_index,sequence,stream,key,content_type,
                metadata,upload,chunk,chunks,total_size,size,sha256,identifier,ciphertext_size,ciphertext_sha256});
            n = n + 1;
        };
    }
    #[test_only]
    public fun item_exists_for_testing(writer: &DeviceData,batch:u64,item:u64):bool {df::exists(&writer.id,ItemKey {batch,item})}
    #[test_only]
    public fun approve_scoped_for_testing(id: vector<u8>, fleet: &FleetData, device_id: ID, stream_hash: vector<u8>, ctx: &TxContext) {
        seal_approve_scoped_reader(id, fleet, device_id, stream_hash, ctx)
    }

    /// Seal accounts only. There is deliberately no device/group approval.
    /// The owner proves current capability custody, so transferring the budget
    /// cap does not leave an implicit grant for the original owner's address.
    entry fun seal_approve_owner(id: vector<u8>, fleet: &FleetData,
        budget: &FleetBudget, cap: &BudgetOwnerCap) {
        owner(fleet, budget, cap);
        assert!(identity_matches(fleet, &id), EAccess);
    }
    /// Read-only Seal approval for managed customer administrators. Do not
    /// borrow/mutate capabilities in an approval PTB: key servers accept only
    /// seal_approve calls with read-only shared inputs.
    entry fun seal_approve_customer(id: vector<u8>, fleet: &FleetData,
        budget: &FleetBudget, customer: &Customer, managed: &Fleet, ctx: &mut TxContext) {
        customer::assert_admin(customer, ctx);
        customer::assert_fleet(customer, managed);
        assert!(fleet.version == VERSION && object::id(budget) == fleet.budget_id
            && customer::fleet_budget(managed) == fleet.budget_id, EBinding);
        assert!(identity_matches(fleet, &id), EAccess);
    }
    entry fun seal_approve_support(id: vector<u8>, fleet: &FleetData, ctx: &TxContext) {
        assert!(identity_matches(fleet, &id) && fleet.support.contains(&ctx.sender()), EAccess);
    }
    public fun identity_matches(fleet: &FleetData, id: &vector<u8>): bool {
        assert!(fleet.version == VERSION, EVersion);
        if (id.length() < 56 || id.length() > 104) return false;
        let mut prefix = object::id(fleet).to_bytes();
        prefix.append(bcs::to_bytes(&fleet.key_epoch));
        let mut i = 0;
        while (i < 40) {
            if (id[i] != prefix[i]) return false;
            i = i + 1;
        };
        true
    }
    fun owner(fleet: &FleetData, budget: &FleetBudget, cap: &BudgetOwnerCap) {
        assert!(fleet.version == VERSION, EVersion);
        assert!(object::id(budget) == fleet.budget_id, EBinding);
        fleet_budget::assert_owner(budget, cap);
    }
    fun device(fleet: &FleetData, writer: &DeviceData, budget: &FleetBudget,
        account: &GasAccount, registry: &DeviceRegistry, cap: &DeviceCap, ctx: &TxContext) {
        assert!(fleet.version == VERSION && writer.version == VERSION, EVersion);
        assert!(writer.fleet_id == object::id(fleet) && object::id(budget) == fleet.budget_id
            && object::id(registry) == fleet.registry_id && object::id(account) == writer.account_id
            && device_cap::device_id(cap) == writer.device_id, EBinding);
        assert!(*df::borrow<DeviceKey, ID>(&fleet.id, DeviceKey { device_id: writer.device_id }) == object::id(writer), EBinding);
        fleet_budget::assert_may_draw(budget, account, registry, cap, ctx);
    }
    public fun writer_id(fleet: &FleetData, device_id: ID): Option<ID> {
        if (!df::exists(&fleet.id, DeviceKey { device_id })) return option::none();
        option::some(*df::borrow(&fleet.id, DeviceKey { device_id }))
    }
    public fun health_seq(writer: &DeviceData): u64 { writer.health_seq }
    public fun anchor_count(writer: &DeviceData): u64 { writer.anchor_count }
    public fun identity_prefix(fleet: &FleetData): vector<u8> {
        let mut prefix = object::id(fleet).to_bytes();
        prefix.append(bcs::to_bytes(&fleet.key_epoch)); prefix
    }
    #[test_only]
    public fun approve_scoped_customer_for_testing(id: vector<u8>, fleet: &FleetData,
        budget: &FleetBudget, customer: &Customer, managed: &Fleet,
        device_id: ID, stream_hash: vector<u8>, ctx: &mut TxContext) {
        seal_approve_scoped_customer(id, fleet, budget, customer, managed, device_id, stream_hash, ctx)
    }
    #[test_only]
    public fun approve_customer_for_testing(id: vector<u8>, fleet: &FleetData, budget: &FleetBudget, customer: &Customer, managed: &Fleet, ctx: &mut TxContext) {
        seal_approve_customer(id, fleet, budget, customer, managed, ctx)
    }
    #[test_only]
    public fun approve_support_for_testing(id: vector<u8>, fleet: &FleetData, ctx: &TxContext) {
        seal_approve_support(id, fleet, ctx)
    }
}
