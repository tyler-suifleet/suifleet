
    // This is the exact storage mutation in grant(), isolated from allocation
    // and transfer effects. Its native absence condition is explicit. It proves
    // framing of registry custody/identity, not all grant() runtime behavior.
    #[mode(spec), ext(spec_only)]
    fun role_add_frame(registry: &mut DeviceRegistry, role_id: ID, operator: address) {
        df::add(device_registry::uid_mut(registry), RoleKey { role_id }, operator);
    }
    #[mode(spec), ext(spec(prove, target = role_add_frame))]
    fun role_add_frame_spec(registry: &mut DeviceRegistry, role_id: ID, operator: address) {
        prover::prover::asserts(!df::exists(device_registry::uid(registry), RoleKey { role_id }));
        role_add_frame(registry, role_id, operator);
        prover::prover::ensures(device_registry::DeviceRegistry_inv(registry));
    }
