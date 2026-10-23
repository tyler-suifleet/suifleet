    // Proof-only observations of the actual typed dynamic-field predicates.
    // These helpers have no executable release body and assume no invariant.
    #[mode(spec), ext(spec_only)]
    public fun spec_typed_identity(registry: &DeviceRegistry, device_id: ID): bool {
        df::exists_with_type<IdentityKey, Identity>(&registry.id, IdentityKey { device_id })
    }



    #[mode(spec), ext(spec(prove, target = set_identity, no_opaque))]
    fun set_identity_security_spec(registry: &mut DeviceRegistry, device_id: ID, identity: Identity) {
        prover::prover::asserts(!df::exists(&registry.id, IdentityKey { device_id })
            || spec_typed_identity(registry, device_id));
        let expected = identity;
        set_identity(registry, device_id, identity);
        prover::prover::ensures(spec_typed_identity(registry, device_id));
        prover::prover::ensures(*identity_of(registry, device_id) == expected);
    }

    #[mode(spec), ext(spec(prove, target = revoke_device))]
    fun revoke_device_security_spec(registry: &mut DeviceRegistry, cap: &DeviceOwnerCap) {
        let device_id = cap.device_id;
        prover::prover::asserts(dof::exists_with_type<CustodyKey, Device>(&registry.id, CustodyKey { device_id }));
        prover::prover::asserts(!df::exists(&registry.id, IdentityKey { device_id }) || spec_typed_identity(registry, device_id));
        revoke_device(registry, cap);
        prover::prover::ensures(spec_typed_identity(registry, device_id));
        prover::prover::ensures(identity_of(registry, device_id).revoked);
    }

    #[mode(spec), ext(spec(prove, target = reinstate_device))]
    fun reinstate_device_security_spec(registry: &mut DeviceRegistry, cap: &DeviceOwnerCap) {
        let device_id = cap.device_id;
        prover::prover::asserts(dof::exists_with_type<CustodyKey, Device>(&registry.id, CustodyKey { device_id }));
        prover::prover::asserts(!df::exists(&registry.id, IdentityKey { device_id }) || spec_typed_identity(registry, device_id));
        reinstate_device(registry, cap);
        prover::prover::ensures(spec_typed_identity(registry, device_id));
        prover::prover::ensures(!identity_of(registry, device_id).revoked);
    }

    #[mode(spec), ext(spec(prove, target = rotate_device_key))]
    fun rotate_device_key_security_spec(registry: &mut DeviceRegistry, cap: &DeviceOwnerCap, new_address: address) {
        let device_id = cap.device_id;
        prover::prover::asserts(dof::exists_with_type<CustodyKey, Device>(&registry.id, CustodyKey { device_id }));
        prover::prover::asserts(spec_typed_identity(registry, device_id));
        let revoked = identity_of(registry, device_id).revoked;
        rotate_device_key(registry, cap, new_address);
        prover::prover::ensures(spec_typed_identity(registry, device_id));
        prover::prover::ensures(identity_of(registry, device_id).revoked == revoked);
        prover::prover::ensures(identity_of(registry, device_id).signer == new_address);
    }

    #[mode(spec), ext(spec(prove, target = borrow_device, no_opaque))]
    fun borrow_device_security_spec(registry: &mut DeviceRegistry, cap: &DeviceOwnerCap): (Device, DeviceLoan) {
        let device_id = cap.device_id;
        prover::prover::asserts(dof::exists_with_type<CustodyKey, Device>(&registry.id, CustodyKey { device_id }));
        let expected_id = object::id(dof::borrow<CustodyKey, Device>(&registry.id, CustodyKey { device_id }));
        let typed = spec_typed_identity(registry, device_id);
        let (device, loan) = borrow_device(registry, cap);
        prover::prover::ensures(object::id(&device) == expected_id);
        prover::prover::ensures(loan.device_id == device_id);
        prover::prover::ensures(spec_typed_identity(registry, device_id) == typed);
        prover::prover::ensures(!dof::exists_with_type<CustodyKey, Device>(&registry.id, CustodyKey { device_id }));
        (device, loan)
    }


