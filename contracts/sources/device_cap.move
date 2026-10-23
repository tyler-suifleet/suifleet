module sui_edge::device_cap {
    use sui_edge::device_registry::{Self, Device, DeviceRegistry};
    use sui::dynamic_field as df;

    const EWrongSigner: u64 = 0;
    const EUnboundLegacyCap: u64 = 1;
    const EInactiveIdentity: u64 = 2;
    public struct DeviceAddressKey has copy, drop, store {}

    // ── Objects ──────────────────────────────────────────────────────────────

    /// Capability owned by the edge device's SUI address.
    /// Authorises that address to submit on-chain status reports.
    public struct DeviceCap has key, store {
        id: UID,
        /// ID of the corresponding Device object.
        device_id: ID,
    }

    // ── Public functions ─────────────────────────────────────────────────────

    /// Issue a cap and transfer it to the device's on-device keypair address.
    /// Called once during device registration.
    public fun issue(device: &Device, ctx: &mut TxContext): DeviceCap {
        let mut cap = DeviceCap {
            id: object::new(ctx),
            device_id: device_registry::device_id(device),
        };
        df::add(&mut cap.id, DeviceAddressKey {}, device_registry::device_address(device));
        cap
    }

    /// Entry point: issue a cap for an existing device and send it to device_address.
    /// Can be called standalone (e.g. for devices registered before cap issuance was
    /// added) or composed into a PTB alongside register_device.
    #[allow(lint(public_entry))]
    public entry fun issue_cap(device: &Device, ctx: &mut TxContext) {
        let cap = issue(device, ctx);
        transfer::transfer(cap, device_registry::device_address(device));
    }

    // ── Read helpers ──────────────────────────────────────────────────────────

    /// Cap possession alone is insufficient: public issuance/transfer must not
    /// let an operator impersonate the registered device signer.
    #[allow(lint(prefer_mut_tx_context))]
    public fun assert_signer(cap: &DeviceCap, ctx: &TxContext) {
        assert!(df::exists(&cap.id, DeviceAddressKey {}), EUnboundLegacyCap);
        let signer: &address = df::borrow(&cap.id, DeviceAddressKey {});
        assert!(ctx.sender() == *signer, EWrongSigner);
    }

    /// The signer check plus the registry's current identity: the device must
    /// be registered or activated, not revoked or deregistered, and the
    /// sender must be its current key (a rotation invalidates older caps).
    #[allow(lint(prefer_mut_tx_context))]
    public fun assert_active(cap: &DeviceCap, registry: &DeviceRegistry, ctx: &TxContext) {
        assert_signer(cap, ctx);
        assert!(device_registry::is_active_signer(registry, cap.device_id, ctx.sender()), EInactiveIdentity);
    }

    public fun device_id(cap: &DeviceCap): ID { cap.device_id }
}
