module sui_edge::device_cap {
    use sui_edge::device_registry::{Self, Device};

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
        DeviceCap {
            id: object::new(ctx),
            device_id: device_registry::device_id(device),
        }
    }

    /// Entry point: issue a cap for an existing device and send it to device_address.
    /// Can be called standalone (e.g. for devices registered before cap issuance was
    /// added) or composed into a PTB alongside register_device.
    public entry fun issue_cap(device: &Device, ctx: &mut TxContext) {
        let cap = issue(device, ctx);
        transfer::transfer(cap, device_registry::device_address(device));
    }

    // ── Read helpers ──────────────────────────────────────────────────────────

    public fun device_id(cap: &DeviceCap): ID { cap.device_id }
}
