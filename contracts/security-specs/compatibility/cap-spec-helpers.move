    #[mode(spec), ext(spec(prove, target = assert_signer))]
    fun assert_signer_security_spec(cap: &DeviceCap, ctx: &TxContext) {
        prover::prover::asserts(df::exists_with_type<DeviceAddressKey, address>(&cap.id, DeviceAddressKey {}));
        let signer: address = *df::borrow(&cap.id, DeviceAddressKey {});
        prover::prover::asserts(ctx.sender() == signer);
        assert_signer(cap, ctx);
    }
