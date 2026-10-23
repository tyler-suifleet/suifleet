
    // Closed-writer representation invariant. This is NOT a native/VM
    // assumption: constructor and every private-key writer must establish and
    // preserve it. Generic raw/native fixtures retain unknown/orphan states.
    #[mode(spec), ext(spec_only, pure)]
    fun spec_registry_at(registry: &DeviceRegistry, device_id: ID): bool {
        (!df::exists(&registry.id, IdentityKey { device_id }) || spec_typed_identity(registry, device_id))
        && (!dof::spec_name_exists_macro!(&registry.id, CustodyKey { device_id })
            || (dof::exists_with_type<CustodyKey, Device>(&registry.id, CustodyKey { device_id })
                && object::id(dof::borrow<CustodyKey, Device>(&registry.id, CustodyKey { device_id })) == device_id
                && spec_typed_identity(registry, device_id)))
    }
    #[mode(spec), ext(spec_only)]
    public fun DeviceRegistry_inv(registry: &DeviceRegistry): bool {
        prover::prover::forall!<ID>(|id| spec_registry_at(registry, *id))
    }
    #[mode(spec), ext(spec(prove, target = security_actual_init, boogie_opt = b"vcsMaxKeepGoingSplits:2 vcsSplitOnEveryAssert vcsFinalAssertTimeout:30"))]
    fun init_security_spec(ctx: &mut TxContext) { security_actual_init(ctx); }

    #[mode(spec), ext(spec(prove, target = register_device, no_opaque))]
    fun register_device_security_spec(registry: &mut DeviceRegistry, name: String,
        device_address: address, arch: String, clock: &Clock, ctx: &mut TxContext): Device {
        prover::prover::asserts(registry.device_count < 18446744073709551615);
        let count = registry.device_count;
        let device = register_device(registry, name, device_address, arch, clock, ctx);
        prover::prover::ensures(registry.device_count == count + 1);
        prover::prover::ensures(!is_custodied(registry, object::id(&device)));
        device
    }
    #[mode(spec), ext(spec(prove, target = deregister_device, no_opaque))]
    fun deregister_device_security_spec(registry: &mut DeviceRegistry, device: Device, ctx: &TxContext) {
        prover::prover::asserts(registry.device_count > 0);
        let id = object::id(&device);
        let existed = df::exists(&registry.id, IdentityKey { device_id: id });
        deregister_device(registry, device, ctx);
        prover::prover::ensures(!existed || spec_typed_identity(registry, id));
        if (spec_typed_identity(registry, id)) {
            prover::prover::ensures(identity_of(registry, id).revoked);
        };
    }
    #[mode(spec), ext(spec(prove, target = deregister_activated_device, no_opaque))]
    fun deregister_activated_security_spec(registry: &mut DeviceRegistry, cap: DeviceOwnerCap, ctx: &TxContext) {
        let id = cap.device_id;
        prover::prover::asserts(registry.device_count > 0);
        prover::prover::asserts(is_custodied(registry, id));
        deregister_activated_device(registry, cap, ctx);
        prover::prover::ensures(!is_custodied(registry, id));
        prover::prover::ensures(spec_typed_identity(registry, id));
        if (spec_typed_identity(registry, id)) {
            prover::prover::ensures(identity_of(registry, id).revoked);
        };
    }
