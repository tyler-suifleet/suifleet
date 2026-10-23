    #[mode(spec), ext(spec(prove, target = activate_device))]
    fun activate_device_security_spec(registry: &mut DeviceRegistry, device: Device, ctx: &mut TxContext): DeviceOwnerCap {
        let device_id = object::id(&device);
        prover::prover::asserts(!df::exists(&registry.id, IdentityKey { device_id })
            || spec_typed_identity(registry, device_id));
        prover::prover::asserts(!dof::spec_name_exists(&registry.id, CustodyKey { device_id }));
        let cap = activate_device(registry, device, ctx);
        prover::prover::ensures(is_custodied(registry, device_id));
        prover::prover::ensures(spec_typed_identity(registry, device_id));
        prover::prover::ensures(!identity_of(registry, device_id).revoked);
        prover::prover::ensures(identity_of(registry, device_id).owner_cap == object::id(&cap));
        cap
    }

    #[mode(spec), ext(spec(prove, target = return_device))]
    fun return_device_security_spec(registry: &mut DeviceRegistry, device: Device, loan: DeviceLoan) {
        let device_id = loan.device_id;
        prover::prover::asserts(object::id(&device) == device_id);
        prover::prover::asserts(spec_typed_identity(registry, device_id));
        prover::prover::asserts(device.device_address == identity_of(registry, device_id).signer);
        prover::prover::asserts(!dof::spec_name_exists(&registry.id, CustodyKey { device_id }));
        return_device(registry, device, loan);
        prover::prover::ensures(spec_typed_identity(registry, device_id));
        prover::prover::ensures(is_custodied(registry, device_id));
    }
