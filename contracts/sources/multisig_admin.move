/// Native Sui multisig authentication; no off-chain service or caller-labelled address.
module sui_edge::multisig_admin {
    const SMALL_34: u8 = 34;
    const SMALL_33: u8 = 33;
    const SMALL_10: u8 = 10;

    // Widen named narrow literals at use sites to avoid repeated LdU64.
    // The pinned compiler uses LdU8/U16/U32 followed by CastU64. Values and u64 types
    // are unchanged; abort constants retain their original u64 declarations.
    const SMALL_0: u8 = 0;
    const SMALL_1: u8 = 1;
    const SMALL_7: u8 = 7;

    use std::bcs;
    use sui::clock::Clock;
    use sui::hash;
    use sui_edge::customer::{Self, Customer};
    use sui_edge::security_policy::{Self, Action};

    const EConfiguration: u64 = 0;
    const EAuthority: u64 = 1;
    const EAction: u64 = 2;

    /// Ordered Sui public keys include their scheme flag (0/1/2/6). The
    /// derivation is flag 3 || threshold LE u16 || (flagged key || weight)*.
    /// Native transaction authentication verifies the threshold before Move runs.
    public fun administrator(keys: &vector<vector<u8>>, weights: &vector<u8>, threshold: u16): address {
        g0(keys.length() > (SMALL_0 as u64)); g0(keys.length() <= (SMALL_10 as u64)); g0(keys.length() == weights.length());
        let mut bytes = vector[3];
        bytes.append(bcs::to_bytes(&threshold));
        let mut total = (SMALL_0 as u64);
        let mut i = (SMALL_0 as u64);
        while (i < keys.length()) {
            let key = &keys[i];
            g0(!key.is_empty());
            let flag = key[(SMALL_0 as u64)];
            g0((flag == 0 && key.length() == (SMALL_33 as u64)) || ((flag == 1 || flag == 2 || flag == 6) && key.length() == (SMALL_34 as u64)));
            let mut j = (SMALL_0 as u64);
            while (j < i) { g0(keys[j] != *key); j = j + (SMALL_1 as u64); };
            g0(weights[i] > 0);
            total = total + (weights[i] as u64);
            bytes.append(*key); bytes.push_back(weights[i]); i = i + (SMALL_1 as u64);
        };
        g0(threshold > 0); g0((threshold as u64) <= total);
        sui::address::from_bytes(hash::blake2b256(&bytes))
    }

    /// First add the derived address as an administrator using current policy.
    /// Enrollment then requires a real transaction from its native quorum.
    /// Reconfiguration uses the current factors, including any other mechanisms.
    public fun enroll(customer: &mut Customer, keys: vector<vector<u8>>, weights: vector<u8>, threshold: u16, ctx: &mut TxContext) {
        customer::assert_security_administrator(customer, ctx);
        let sender = administrator(&keys, &weights, threshold);
        g1(ctx.sender() == sender);
        customer::configure_security_factor(customer, 1, p0(sender, keys, weights, threshold), ctx);
    }

    /// Convenience producer for the exact existing policy-change wrapper.
    public fun prepare_policy_change(customer: &Customer, requirements: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[bcs::to_bytes(&b"set_security"), bcs::to_bytes(&object::id(customer)), bcs::to_bytes(&requirements)];
        customer::prepare_security_action(customer, security_policy::action_intent(object::id(customer), bcs::to_bytes(&arguments)), expires_ms, clock, ctx);
    }

    public fun enroll_secured(customer: &mut Customer, keys: vector<vector<u8>>, weights: vector<u8>, threshold: u16, action: Action, clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[bcs::to_bytes(&b"enroll_multisig"), bcs::to_bytes(&object::id(customer)), bcs::to_bytes(&keys), bcs::to_bytes(&weights), bcs::to_bytes(&threshold)];
        customer::begin_security_action(customer, action, arguments, (SMALL_7 as u64), false, clock, ctx);
        enroll(customer, keys, weights, threshold, ctx);
        customer::finish_security_action(customer);
    }

    /// Attest the exact immutable action, not a browser approval flag. Revoked
    /// administrators, replaced configurations and stale generations cannot attest.
    public fun approve(customer: &Customer, action: &mut Action, keys: vector<vector<u8>>, weights: vector<u8>, threshold: u16, clock: &Clock, ctx: &mut TxContext) {
        customer::assert_security_administrator(customer, ctx);
        let sender = administrator(&keys, &weights, threshold);
        g1(ctx.sender() == sender);
        g1(customer::security_factor_configuration(customer, 1) == p0(sender, keys, weights, threshold));
        g2(security_policy::customer_id(action) == object::id(customer)); g2(security_policy::action_generation(action) == security_policy::generation(&customer::security(customer))); g2(clock.timestamp_ms() < security_policy::expires_ms(action));
        security_policy::attest(action, 1);
    }
    /// binding: compact private symbol to bound published bytecode storage.
    fun p0(sender: address, keys: vector<vector<u8>>, weights: vector<u8>, threshold: u16): vector<u8> {
        let mut bytes = bcs::to_bytes(&sender);
        bytes.append(bcs::to_bytes(&keys)); bytes.append(bcs::to_bytes(&weights));
        bytes.append(bcs::to_bytes(&threshold)); bytes
    }

    /// Shared EConfiguration guard; preserve evaluation order and abort code.
    fun g0(condition: bool) { if (!condition) abort EConfiguration }
    /// Shared EAuthority guard; preserve evaluation order and abort code.
    fun g1(condition: bool) { if (!condition) abort EAuthority }
    /// Shared EAction guard; preserve evaluation order and abort code.
    fun g2(condition: bool) { if (!condition) abort EAction }
}
