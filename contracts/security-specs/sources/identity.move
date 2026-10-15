// Unverified until the release-framework compatibility blocker is repaired.
module fleet_security_specs::identity;

use sui_edge::device_registry::{Self, DeviceRegistry};
use prover::prover::{requires, ensures};

#[mode(spec), ext(spec(prove, target = sui_edge::device_registry::is_active_signer))]
fun is_active_signer_spec(registry: &DeviceRegistry, device_id: ID, signer: address): bool {
    let custodied = device_registry::is_custodied(registry, device_id);
    let identity = device_registry::identity(registry, device_id);
    // Reachable custody state has an identity, created by activate_device.
    // Establishing that representation invariant is a separate proof obligation.
    requires(!custodied || identity.is_some());
    let result = device_registry::is_active_signer(registry, device_id, signer);
    if (!custodied) {
        ensures(!result);
    } else {
        let identity = identity.borrow();
        ensures(result == (!device_registry::identity_revoked(identity)
            && device_registry::identity_signer(identity) == signer));
    };
    result
}
