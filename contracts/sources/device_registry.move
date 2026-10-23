module sui_edge::device_registry {
    use std::string::String;
    use sui::clock::Clock;
    use sui::dynamic_field as df;
    use sui::dynamic_object_field as dof;
    use sui::event;
    use sui::vec_map::{Self, VecMap};

    // ── Constants ─────────────────────────────────────────────────────────────

    const SCHEMA_VERSION: u64 = 1;

    // ── Errors ────────────────────────────────────────────────────────────────

    const ENotCustodied: u64 = 0;
    const EWrongDevice: u64 = 1;

    // ── Objects ───────────────────────────────────────────────────────────────

    /// Shared singleton. Created once at publish time.
    public struct DeviceRegistry has key {
        id: UID,
        admin: address,
        device_count: u64,
        /// Monotonic version bump used to gate future migrations.
        schema_version: u64,
    }

    /// Owned by the wallet that registered the device until it is activated;
    /// from then on held by the registry (see `CustodyKey`).
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

    // Older package versions stay callable and their `deregister_device`
    // deletes any Device passed by value without touching identities. Only a
    // device in registry custody, which those versions cannot reach, is trusted.
    // These types are introduced by an upgrade.

    /// Dynamic object field on the registry holding an activated Device.
    public struct CustodyKey has copy, drop, store { device_id: ID }

    /// Ownership of an activated (custodied) device. Whoever holds it owns the
    /// device: transferring it transfers ownership and all lifecycle authority.
    public struct DeviceOwnerCap has key, store {
        id: UID,
        device_id: ID,
    }

    /// Hot potato for `borrow_device`: the Device must go back into custody in
    /// the same transaction, so it can never be deleted or moved out unchecked.
    public struct DeviceLoan { device_id: ID }

    /// Who may currently report for a device, kept on the shared registry so the
    /// owner can change it without the device's (device-owned) cap.
    public struct IdentityKey has copy, drop, store { device_id: ID }

    public struct Identity has store, copy, drop {
        /// The only address whose status reports are trusted for this device.
        signer: address,
        /// Set by revocation and deregistration (a permanent tombstone).
        revoked: bool,
        /// The DeviceOwnerCap: its owner is the device's operator, the only
        /// address whose deployments the device applies.
        owner_cap: ID,
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

    /// Every identity change: activation, rotation, revocation, reinstatement,
    /// deregistration.
    public struct DeviceIdentityChanged has copy, drop {
        device_id: ID,
        signer: address,
        revoked: bool,
        owner_cap: ID,
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

        // Untrusted until `activate_device` takes it into custody.
        registry.device_count = registry.device_count + 1;
        device
    }

    #[allow(lint(prefer_mut_tx_context, public_entry))]
    public entry fun deregister_device(
        registry: &mut DeviceRegistry,
        device: Device,
        ctx: &TxContext,
    ) {
        // Ownership is proven by passing Device by value — no admin check needed.
        // A custodied Device can only be passed here while borrowed, and then
        // its loan can never be repaid, so the transaction aborts.
        let device_id = object::id(&device);
        if (df::exists(&registry.id, IdentityKey { device_id })) {
            let owner_cap = identity_of(registry, device_id).owner_cap;
            set_identity(registry, device_id, Identity { signer: device.device_address, revoked: true, owner_cap });
        };
        let Device { id, name: _, device_address: _, arch: _, meta: _, registered_at: _, schema_version: _ } = device;

        event::emit(DeviceDeregistered {
            device_id: id.to_inner(),
            owner: ctx.sender(),
        });

        registry.device_count = registry.device_count - 1;
        id.delete();
    }

    #[allow(lint(prefer_mut_tx_context))]
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

    /// Take the Device into registry custody and trust its current key. Required
    /// for every device, including those registered before identities existed;
    /// their existing signer-bound caps keep working.
    public fun activate_device(registry: &mut DeviceRegistry, device: Device, ctx: &mut TxContext): DeviceOwnerCap {
        let device_id = object::id(&device);
        let cap = DeviceOwnerCap { id: object::new(ctx), device_id };
        set_identity(registry, device_id, Identity { signer: device.device_address, revoked: false, owner_cap: object::id(&cap) });
        dof::add(&mut registry.id, CustodyKey { device_id }, device);
        cap
    }

    /// Stop trusting any report from the device until it is reinstated.
    public fun revoke_device(registry: &mut DeviceRegistry, cap: &DeviceOwnerCap) {
        let signer = custodied(registry, cap).device_address;
        set_identity(registry, cap.device_id, Identity { signer, revoked: true, owner_cap: object::id(cap) });
    }

    /// Trust the device's current key again after a revocation.
    public fun reinstate_device(registry: &mut DeviceRegistry, cap: &DeviceOwnerCap) {
        let signer = custodied(registry, cap).device_address;
        set_identity(registry, cap.device_id, Identity { signer, revoked: false, owner_cap: object::id(cap) });
    }

    /// Move the device to a new key. Every cap bound to the old key stops being
    /// accepted; issue a new cap for the new address afterwards. A revoked
    /// device stays revoked until it is reinstated.
    public fun rotate_device_key(registry: &mut DeviceRegistry, cap: &DeviceOwnerCap, new_address: address) {
        assert!(dof::exists(&registry.id, CustodyKey { device_id: cap.device_id }), ENotCustodied);
        let device: &mut Device = dof::borrow_mut(&mut registry.id, CustodyKey { device_id: cap.device_id });
        device.device_address = new_address;
        let revoked = identity_of(registry, cap.device_id).revoked;
        set_identity(registry, cap.device_id, Identity { signer: new_address, revoked, owner_cap: object::id(cap) });
    }

    /// Deregister an activated device: leaves a revoked tombstone, so caps never
    /// outlive the device.
    #[allow(lint(prefer_mut_tx_context))]
    public fun deregister_activated_device(registry: &mut DeviceRegistry, cap: DeviceOwnerCap, ctx: &TxContext) {
        let DeviceOwnerCap { id, device_id } = cap;
        id.delete();
        assert!(dof::exists(&registry.id, CustodyKey { device_id }), ENotCustodied);
        let device: Device = dof::remove(&mut registry.id, CustodyKey { device_id });
        deregister_device(registry, device, ctx);
    }

    /// Lend the custodied Device to calls that take `&Device`/`&mut Device`
    /// (`device_cap::issue_cap`, `device_group::add_device`, `set_meta`).
    public fun borrow_device(registry: &mut DeviceRegistry, cap: &DeviceOwnerCap): (Device, DeviceLoan) {
        assert!(dof::exists(&registry.id, CustodyKey { device_id: cap.device_id }), ENotCustodied);
        let device = dof::remove(&mut registry.id, CustodyKey { device_id: cap.device_id });
        (device, DeviceLoan { device_id: cap.device_id })
    }

    public fun return_device(registry: &mut DeviceRegistry, device: Device, loan: DeviceLoan) {
        let DeviceLoan { device_id } = loan;
        assert!(object::id(&device) == device_id, EWrongDevice);
        // The key cannot change while borrowed: only rotation updates it.
        assert!(device.device_address == identity_of(registry, device_id).signer, EWrongDevice);
        dof::add(&mut registry.id, CustodyKey { device_id }, device);
    }

    /// Whether `signer` may currently report for `device_id`: the Device is
    /// alive in custody and its identity is unrevoked with this key. Devices
    /// never activated are untrusted.
    public fun is_active_signer(registry: &DeviceRegistry, device_id: ID, signer: address): bool {
        if (!dof::exists(&registry.id, CustodyKey { device_id })) return false;
        let identity = identity_of(registry, device_id);
        !identity.revoked && identity.signer == signer
    }

    fun custodied(registry: &DeviceRegistry, cap: &DeviceOwnerCap): &Device {
        assert!(dof::exists(&registry.id, CustodyKey { device_id: cap.device_id }), ENotCustodied);
        dof::borrow(&registry.id, CustodyKey { device_id: cap.device_id })
    }

    fun identity_of(registry: &DeviceRegistry, device_id: ID): &Identity {
        df::borrow(&registry.id, IdentityKey { device_id })
    }

    fun set_identity(registry: &mut DeviceRegistry, device_id: ID, identity: Identity) {
        let Identity { signer, revoked, owner_cap } = identity;
        if (df::exists(&registry.id, IdentityKey { device_id })) {
            *df::borrow_mut(&mut registry.id, IdentityKey { device_id }) = identity;
        } else {
            df::add(&mut registry.id, IdentityKey { device_id }, identity);
        };
        event::emit(DeviceIdentityChanged { device_id, signer, revoked, owner_cap });
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
    public fun identity_revoked(identity: &Identity): bool { identity.revoked }
    public fun identity_owner_cap(identity: &Identity): ID { identity.owner_cap }
    public fun is_custodied(registry: &DeviceRegistry, device_id: ID): bool {
        dof::exists(&registry.id, CustodyKey { device_id })
    }
    public fun custodied_device(registry: &DeviceRegistry, device_id: ID): &Device {
        dof::borrow(&registry.id, CustodyKey { device_id })
    }
    public fun owner_cap_device_id(cap: &DeviceOwnerCap): ID { cap.device_id }

    /// For package modules that keep state on the registry (`roles`).
    public(package) fun uid(registry: &DeviceRegistry): &UID { &registry.id }
    public(package) fun uid_mut(registry: &mut DeviceRegistry): &mut UID { &mut registry.id }
}
