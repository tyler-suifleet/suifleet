// Proof obligations target the implementation, not a copied transition model.
module fleet_security_specs::enrollment;

use sui_edge::enrollment::{Self, EnrollmentPolicy};
use prover::prover::{asserts, ensures};

#[mode(spec), ext(spec(prove, target = sui_edge::enrollment::close))]
fun close_spec(policy: &mut EnrollmentPolicy, ctx: &TxContext) {
    // `asserts` specifies the exact successful-path condition, including aborts.
    asserts(ctx.sender() == enrollment::operator(policy));
    let remaining = enrollment::remaining(policy);
    let enrolled = enrollment::enrolled(policy);
    enrollment::close(policy, ctx);
    ensures(!enrollment::is_open(policy));
    ensures(enrollment::remaining(policy) == remaining);
    ensures(enrollment::enrolled(policy) == enrolled);
}

#[mode(spec), ext(spec(prove, target = sui_edge::enrollment::set_limits))]
fun set_limits_spec(policy: &mut EnrollmentPolicy, quota: u64, expires: u64, ctx: &TxContext) {
    asserts(ctx.sender() == enrollment::operator(policy));
    let open = enrollment::is_open(policy);
    let enrolled = enrollment::enrolled(policy);
    enrollment::set_limits(policy, quota, expires, ctx);
    ensures(enrollment::remaining(policy) == quota);
    ensures(enrollment::is_open(policy) == open);
    ensures(enrollment::enrolled(policy) == enrolled);
}
