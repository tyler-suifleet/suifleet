    #[mode(spec), ext(spec(prove, target = revoke_claim))]
    fun revoke_claim_security_spec(policy: &mut EnrollmentPolicy, claim: address, ctx: &TxContext) {
        prover::prover::asserts(ctx.sender() == policy.operator);
        prover::prover::asserts(policy.claims.contains(claim));
        let remaining = policy.remaining;
        revoke_claim(policy, claim, ctx);
        prover::prover::ensures(!policy.claims.contains(claim));
        prover::prover::ensures(policy.remaining == remaining);
    }

