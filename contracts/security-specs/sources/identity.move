// Exact arbitrary-state abort/return behavior; no reachable-storage assumption.
module fleet_security_specs::identity;

use sui_edge::device_registry::{Self, DeviceRegistry};
use prover::prover::{asserts, ensures};

#[mode(spec), ext(spec(prove, target = sui_edge::device_registry::is_active_signer))]
fun is_active_signer_spec(registry: &DeviceRegistry, device_id: ID, signer: address): bool {
    let custodied = device_registry::is_custodied(registry, device_id);
    let typed_identity = device_registry::spec_typed_identity(registry, device_id);
    asserts(!custodied || typed_identity);
    let result = device_registry::is_active_signer(registry, device_id, signer);
    if (!custodied) {
        ensures(!result);
    } else {
        let identity = device_registry::identity(registry, device_id).destroy_some();
        ensures(result == (!device_registry::identity_revoked(&identity)
            && device_registry::identity_signer(&identity) == signer));
    };
    result
}
