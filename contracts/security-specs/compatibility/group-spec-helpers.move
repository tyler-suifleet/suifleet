    #[mode(spec), ext(spec(prove, target = k0))]
    fun handover_security_spec(group: &mut DeviceGroup, recipient: address, ctx: &TxContext) {
        prover::prover::asserts(ctx.sender() == group.admin);
        let devices = group.device_ids;
        k0(group, recipient, ctx);
        prover::prover::ensures(group.admin == recipient);
        prover::prover::ensures(group.device_ids == devices);
    }
