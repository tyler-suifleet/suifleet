    #[mode(spec), ext(spec(prove, target = enroll))]
    fun enroll_security_spec(policy: &mut EnrollmentPolicy, registry: &mut DeviceRegistry,
                            name: String, arch: String, clock: &Clock, ctx: &mut TxContext) {
        prover::ghost::declare_global_mut<specs::transfer_spec::SpecTransferAddressExists, bool>();
        prover::ghost::declare_global_mut<specs::transfer_spec::SpecTransferAddress, address>();
        prover::prover::asserts(object::id(registry) == policy.registry);
        prover::prover::asserts(policy.open);
        prover::prover::asserts(policy.expires_at_ms == 0 || clock.timestamp_ms() <= policy.expires_at_ms);
        prover::prover::asserts(policy.remaining > 0);
        prover::prover::asserts(ctx.sponsor().is_some());
        let claim = ctx.sponsor().destroy_some();
        prover::prover::asserts(claim != ctx.sender());
        prover::prover::asserts(policy.claims.contains(claim));
        prover::prover::asserts(policy.enrolled < 18446744073709551615);
        prover::prover::asserts(device_registry::device_count(registry) < 18446744073709551615);
        let remaining = policy.remaining;
        let enrolled = policy.enrolled;
        prover::prover::boogie_split_here();
        enroll(policy, registry, name, arch, clock, ctx);
        prover::prover::boogie_split_here();
        prover::prover::ensures(policy.remaining == remaining - 1);
        prover::prover::ensures(policy.enrolled == enrolled + 1);
        prover::prover::ensures(!policy.claims.contains(claim));
    }
