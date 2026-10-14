module sui_edge::device_registry {
    use std::string::String;
    use sui::clock::Clock;
    use sui::dynamic_field as df;
    use sui::event;
    use sui::vec_map::{Self, VecMap};

    // ── Constants ─────────────────────────────────────────────────────────────

    const SCHEMA_VERSION: u64 = 1;

    // ── Errors ────────────────────────────────────────────────────────────────

    const ENoIdentity: u64 = 0;
    const ENotOperator: u64 = 1;

    // ── Objects ───────────────────────────────────────────────────────────────

    /// Shared singleton. Created once at publish time.
    public struct DeviceRegistry has key {
        id: UID,
        admin: address,
        device_count: u64,
        /// Monotonic version bump used to gate future migrations.
        schema_version: u64,
    }

    /// Owned by the wallet that registered the device.
    public struct Device has key, store {
        id: UID,
        name: String,
        /// SUI address of the keypair running on the edge device itself.
        device_address: address,
        arch: String,
        /// Open-ended key/value metadata — extend without struct changes.
        meta: VecMap<String, String>,
        registered_at: u64,
        schema_version: u64,
    }

    /// Who may currently act for a device, kept on the shared registry so the
    /// operator can change it without the device's (device-owned) cap.
    /// Introduced by an upgrade: older package versions cannot read or write it.
    public struct IdentityKey has copy, drop, store { device_id: ID }

    public struct Identity has store, copy, drop {
        /// The only address whose status reports are trusted for this device.
        signer: address,
        /// Address that last registered, activated or rotated the device; it
        /// may revoke by ID, even after the Device object is gone.
        operator: address,
        revoked: bool,
    }

    // ── Events ────────────────────────────────────────────────────────────────

    public struct DeviceRegistered has copy, drop {
        device_id: ID,
        name: String,
        device_address: address,
        owner: address,
    }

    public struct DeviceDeregistered has copy, drop {
        device_id: ID,
        owner: address,
    }

    /// Every identity change: registration, activation, rotation, revocation.
    public struct DeviceIdentityChanged has copy, drop {
        device_id: ID,
        signer: address,
        operator: address,
        revoked: bool,
    }

    public struct RegistryCreated has copy, drop {
        registry_id: ID,
        admin: address,
    }

    // ── Init ──────────────────────────────────────────────────────────────────

    fun init(ctx: &mut TxContext) {
        let registry = DeviceRegistry {
            id: object::new(ctx),
            admin: ctx.sender(),
            device_count: 0,
            schema_version: SCHEMA_VERSION,
        };
        event::emit(RegistryCreated {
            registry_id: object::id(&registry),
            admin: ctx.sender(),
        });
        transfer::share_object(registry);
    }

    #[test_only]
    public fun init_for_testing(ctx: &mut TxContext) {
        init(ctx);
    }

    /// Replays a device registered before identities existed.
    #[test_only]
    public fun remove_identity_for_testing(registry: &mut DeviceRegistry, device_id: ID) {
        let _: Identity = df::remove(&mut registry.id, IdentityKey { device_id });
    }

    // ── Public functions ──────────────────────────────────────────────────────

    public fun register_device(
        registry: &mut DeviceRegistry,
        name: String,
        device_address: address,
        arch: String,
        clock: &Clock,
        ctx: &mut TxContext,
    ): Device {
        let device = Device {
            id: object::new(ctx),
            name,
            device_address,
            arch,
            meta: vec_map::empty(),
            registered_at: clock.timestamp_ms(),
            schema_version: SCHEMA_VERSION,
        };

        event::emit(DeviceRegistered {
            device_id: object::id(&device),
            name: device.name,
            device_address,
            owner: ctx.sender(),
        });

        registry.device_count = registry.device_count + 1;
        set_identity(registry, object::id(&device), device_address, ctx.sender(), false);
        device
    }

    public entry fun deregister_device(
        registry: &mut DeviceRegistry,
        device: Device,
        ctx: &TxContext,
    ) {
        // Ownership is proven by passing Device by value — no admin check needed.
        // The identity stays as a revoked tombstone: caps never outlive the device.
        let device_id = object::id(&device);
        set_identity(registry, device_id, device.device_address, ctx.sender(), true);
        let Device { id, name: _, device_address: _, arch: _, meta: _, registered_at: _, schema_version: _ } = device;

        event::emit(DeviceDeregistered {
            device_id: id.to_inner(),
            owner: ctx.sender(),
        });

        registry.device_count = registry.device_count - 1;
        id.delete();
    }

    public fun set_meta(
        device: &mut Device,
        key: String,
        value: String,
        _ctx: &TxContext,
    ) {
        if (device.meta.contains(&key)) {
            let (_k, _v) = device.meta.remove(&key);
        };
        device.meta.insert(key, value);
    }

    // ── Identity lifecycle ────────────────────────────────────────────────────

    /// Trust the device's current key: for devices registered before
    /// identities existed, after an ownership transfer (the new owner becomes
    /// the operator), or to reinstate a revoked device. Only the Device's
    /// owner can pass it.
    public fun activate_device(registry: &mut DeviceRegistry, device: &Device, ctx: &TxContext) {
        set_identity(registry, object::id(device), device.device_address, ctx.sender(), false);
    }

    /// Move the device to a new key. Every cap bound to the old key stops
    /// being accepted; issue a new cap for the new address afterwards.
    public fun rotate_device_key(
        registry: &mut DeviceRegistry,
        device: &mut Device,
        new_address: address,
        ctx: &TxContext,
    ) {
        device.device_address = new_address;
        set_identity(registry, object::id(device), new_address, ctx.sender(), false);
    }

    /// Stop trusting any report from the device. By ID, so the recorded
    /// operator can still revoke after the Device was deleted or transferred.
    public fun revoke_device(registry: &mut DeviceRegistry, device_id: ID, ctx: &TxContext) {
        assert!(df::exists(&registry.id, IdentityKey { device_id }), ENoIdentity);
        let identity: &Identity = df::borrow(&registry.id, IdentityKey { device_id });
        assert!(identity.operator == ctx.sender(), ENotOperator);
        let signer = identity.signer;
        set_identity(registry, device_id, signer, ctx.sender(), true);
    }

    /// Whether `signer` may currently report for `device_id`. Devices without
    /// an identity (registered before identities existed) are not trusted
    /// until their owner calls `activate_device`.
    public fun is_active_signer(registry: &DeviceRegistry, device_id: ID, signer: address): bool {
        if (!df::exists(&registry.id, IdentityKey { device_id })) return false;
        let identity: &Identity = df::borrow(&registry.id, IdentityKey { device_id });
        !identity.revoked && identity.signer == signer
    }

    fun set_identity(registry: &mut DeviceRegistry, device_id: ID, signer: address, operator: address, revoked: bool) {
        let identity = Identity { signer, operator, revoked };
        if (df::exists(&registry.id, IdentityKey { device_id })) {
            *df::borrow_mut(&mut registry.id, IdentityKey { device_id }) = identity;
        } else {
            df::add(&mut registry.id, IdentityKey { device_id }, identity);
        };
        event::emit(DeviceIdentityChanged { device_id, signer, operator, revoked });
    }

    // ── Read helpers ──────────────────────────────────────────────────────────

    public fun device_id(device: &Device): ID { object::id(device) }
    public fun device_address(device: &Device): address { device.device_address }
    public fun device_count(registry: &DeviceRegistry): u64 { registry.device_count }
    public fun identity(registry: &DeviceRegistry, device_id: ID): Option<Identity> {
        if (!df::exists(&registry.id, IdentityKey { device_id })) return option::none();
        option::some(*df::borrow(&registry.id, IdentityKey { device_id }))
    }
    public fun identity_signer(identity: &Identity): address { identity.signer }
    public fun identity_operator(identity: &Identity): address { identity.operator }
    public fun identity_revoked(identity: &Identity): bool { identity.revoked }
}
