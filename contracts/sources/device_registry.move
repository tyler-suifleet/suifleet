module sui_edge::device_registry {
    use std::string::String;
    use sui::clock::Clock;
    use sui::event;
    use sui::vec_map::{Self, VecMap};

    // ── Errors ──────────────────────────────────────────────────────────────

    // ── Objects ──────────────────────────────────────────────────────────────

    /// Shared singleton. Created once at publish time.
    public struct DeviceRegistry has key {
        id: UID,
        admin: address,
        device_count: u64,
    }

    /// Owned by the wallet that registered the device.
    public struct Device has key, store {
        id: UID,
        name: String,
        /// SUI address of the keypair running on the edge device itself.
        device_address: address,
        arch: String,
        meta: VecMap<String, String>,
        registered_at: u64,
    }

    // ── Events ───────────────────────────────────────────────────────────────

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

    // ── Init ─────────────────────────────────────────────────────────────────

    fun init(ctx: &mut TxContext) {
        transfer::share_object(DeviceRegistry {
            id: object::new(ctx),
            admin: ctx.sender(),
            device_count: 0,
        });
    }

    #[test_only]
    public fun init_for_testing(ctx: &mut TxContext) {
        init(ctx);
    }

    // ── Public functions ─────────────────────────────────────────────────────

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
        };

        event::emit(DeviceRegistered {
            device_id: object::id(&device),
            name: device.name,
            device_address,
            owner: ctx.sender(),
        });

        registry.device_count = registry.device_count + 1;
        device
    }

    public entry fun deregister_device(
        registry: &mut DeviceRegistry,
        device: Device,
        ctx: &TxContext,
    ) {
        // Ownership is proven by passing Device by value — no admin check needed.
        let Device { id, name: _, device_address: _, arch: _, meta: _, registered_at: _ } = device;

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
        ctx: &TxContext,
    ) {
        // Only the device owner (caller) can update metadata.
        let _ = ctx;
        if (device.meta.contains(&key)) {
            let (_k, _v) = device.meta.remove(&key);
        };
        device.meta.insert(key, value);
    }

    // ── Read helpers ──────────────────────────────────────────────────────────

    public fun device_id(device: &Device): ID { object::id(device) }
    public fun device_address(device: &Device): address { device.device_address }
    public fun device_count(registry: &DeviceRegistry): u64 { registry.device_count }
}
