    #[mode(spec), ext(spec(prove, target = revoke))]
    fun revoke_security_spec(registry: &mut DeviceRegistry, role_id: ID, ctx: &TxContext) {
        let key = RoleKey { role_id };
        let typed = df::exists_with_type<RoleKey, address>(device_registry::uid(registry), key);
        prover::prover::asserts(typed);
        let operator: address = *df::borrow(device_registry::uid(registry), key);
        prover::prover::asserts(ctx.sender() == operator);
        revoke(registry, role_id, ctx);
        prover::prover::ensures(!df::exists_with_type<RoleKey, address>(device_registry::uid(registry), key));
        prover::prover::ensures(!df::exists(device_registry::uid(registry), key));
    }

    #[mode(spec), ext(spec(prove, target = is_active))]
    fun is_active_security_spec(registry: &DeviceRegistry, role: &DeployerRole): bool {
        let key = RoleKey { role_id: object::id(role) };
        let present = df::exists(device_registry::uid(registry), key);
        prover::prover::asserts(!present || df::exists_with_type<RoleKey, address>(device_registry::uid(registry), key));
        let result = is_active(registry, role);
        if (!present) prover::prover::ensures(!result)
        else prover::prover::ensures(result == (*df::borrow<RoleKey, address>(device_registry::uid(registry), key) == role.operator));
        result
    }
